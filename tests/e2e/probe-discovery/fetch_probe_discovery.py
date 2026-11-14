"""Probe and discovery review fetch: run production CLI entries and save raw outputs.

Calls libref.cli.main directly so the saved text matches what users see.
Offline probe-repo cases replay bench source archives locally without network.
Online probe-docs and discovery cases need real network; failures are recorded
as SKIP markers rather than regressions.

Usage:
    uv run python tests/e2e/probe-discovery/fetch_probe_discovery.py
    uv run python tests/e2e/probe-discovery/fetch_probe_discovery.py --only httpx-code
    uv run python tests/e2e/probe-discovery/fetch_probe_discovery.py --skip-network
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases.json"
DEFAULT_OUT = HERE / "outputs"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="fetch probe/discovery review outputs")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--only", action="append", default=None)
    parser.add_argument("--skip-network", action="store_true")
    return parser.parse_args()


def _run_cli(argv: list[str], db_path: Path) -> tuple[int, str, str, float]:
    from libref.cli import main

    old = os.environ.get("LIBREF_DB_PATH")
    os.environ["LIBREF_DB_PATH"] = str(db_path)
    for mod in ("libref.services.code.probe", "libref.services.docs.probe"):
        try:
            cache = __import__(mod, fromlist=["_PROBE_CACHE"])
            holder = getattr(cache, "_PROBE_CACHE", None) or getattr(
                cache, "_DOC_PROBE_CACHE", None
            )
            if isinstance(holder, dict):
                holder.clear()
        except ImportError:
            pass
    try:
        from libref.crawler.url_seeder import _SEED_CACHE

        _SEED_CACHE.clear()
    except ImportError:
        pass
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    started = time.time()
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = int(main(argv))
    except SystemExit as exc:
        code = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 — record unexpected failures as text
        err_buf.write(f"failed: {type(exc).__name__}: {exc}\n")
        code = 1
    finally:
        if old is None:
            os.environ.pop("LIBREF_DB_PATH", None)
        else:
            os.environ["LIBREF_DB_PATH"] = old
    return code, out_buf.getvalue(), err_buf.getvalue(), time.time() - started


def _write(out_dir: Path, case_id: str, argv: list[str], result: tuple) -> None:
    code, stdout, stderr, elapsed = result
    (out_dir / f"{case_id}.stdout.txt").write_text(stdout, encoding="utf-8")
    (out_dir / f"{case_id}.meta.txt").write_text(
        "\n".join(
            [
                f"argv: libref {' '.join(argv)}",
                f"exit: {code}",
                f"elapsed: {elapsed:.1f}s",
                "stderr:",
                stderr.strip() or "(empty)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _summarize_probe_repo(payload: str) -> str:
    try:
        data = json.loads(payload)
    except ValueError:
        return "unparsed"
    parts = [
        f"effective={data.get('effective_files')}/{data.get('total_candidates')}",
        f"suffixes={len(data.get('suffix_groups', []))}",
        f"top_dirs={len(data.get('top_dirs', []))}",
        f"samples={len(data.get('samples', []))}",
        f"deps={len(data.get('manifest_deps', []))}",
    ]
    return " ".join(parts)


def _summarize_probe_docs(payload: str) -> str:
    try:
        data = json.loads(payload)
    except ValueError:
        return "unparsed"
    return (
        f"source={data.get('source')} "
        f"total={data.get('total_discovered')} "
        f"accepted={data.get('accepted')} "
        f"samples={len(data.get('samples', []))} "
        f"errors={len(data.get('errors', {}))}"
    )


def _summarize_discovery(payload: str) -> str:
    lines = [line for line in payload.splitlines() if line.strip().startswith("{")]
    if not lines:
        return f"lines=0 stdout_head={payload[:80]!r}"
    first = json.loads(lines[0])
    return f"candidates={len(lines)} first_keys={sorted(first.keys())}"


def main() -> int:
    from tests.e2e.bench import archive as bench_archive

    args = _parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    wanted = set(args.only or [])
    lines: list[str] = [f"cases={CASES}"]
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "probe-review.db"
        db_path.touch()
        spec = {bench_archive.fixture_id(e): e for e in bench_archive.load_spec()}
        for entry in cases.get("probe_repo", []):
            if wanted and entry["id"] not in wanted:
                continue
            bench_entry = spec.get(entry["bench"])
            if bench_entry is None:
                raise SystemExit(f"unknown bench fixture: {entry['bench']}")
            meta = bench_archive.read_meta(bench_entry)
            payload = bench_archive.payload_path(bench_entry, meta)
            root = bench_archive.extract_source(payload, Path(tmp) / entry["id"])
            recipe = bench_archive.recipe(bench_entry)
            argv = ["probe-repo", str(root)]
            if recipe.get("subdir"):
                argv += ["--subdir", str(recipe["subdir"])]
            for pattern in recipe.get("exclude_patterns") or []:
                argv += ["--exclude-pattern", pattern]
            argv += [
                "--tree-depth",
                str(entry.get("tree_depth", 2)),
                "--samples-per-dir",
                str(entry.get("samples_per_dir", 2)),
                "--sample-limit",
                str(entry.get("sample_limit", 20)),
            ]
            result = _run_cli(argv, db_path)
            _write(out_dir, entry["id"], argv, result)
            lines.append(
                f"{entry['id']} bench={entry['bench']} exit={result[0]} "
                + _summarize_probe_repo(result[1])
            )
        if not args.skip_network:
            for entry in cases.get("probe_docs", []):
                if wanted and entry["id"] not in wanted:
                    continue
                bench_entry = spec.get(entry["bench"])
                if bench_entry is None:
                    raise SystemExit(f"unknown bench fixture: {entry['bench']}")
                argv = ["probe-docs", str(bench_entry["start_url"])]
                argv += [
                    "--sample-limit",
                    str(entry.get("sample_limit", 10)),
                    "--fetch-sample-limit",
                    str(entry.get("fetch_sample_limit", 3)),
                ]
                for prefix in bench_entry.get("path_prefixes") or []:
                    argv += ["--path-prefix", prefix]
                if bench_entry.get("max_pages"):
                    argv += ["--max-pages", str(bench_entry["max_pages"])]
                result = _run_cli(argv, db_path)
                _write(out_dir, entry["id"], argv, result)
                lines.append(
                    f"{entry['id']} bench={entry['bench']} exit={result[0]} "
                    + _summarize_probe_docs(result[1])
                )
            for entry in cases.get("discovery", []):
                if wanted and entry["id"] not in wanted:
                    continue
                argv = list(entry["argv"])
                result = _run_cli(argv, db_path)
                _write(out_dir, entry["id"], argv, result)
                lines.append(
                    f"{entry['id']} exit={result[0]} " + _summarize_discovery(result[1])
                )
    (out_dir / "SUMMARY.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
