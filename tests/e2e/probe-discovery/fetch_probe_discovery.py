"""Probe and discovery review fetch: run production CLI entries and save raw outputs.

Local repository probes use the server-local CLI so host paths stay on the server.
Documentation probes and discovery use librefctl against an in-process Web app,
which exercises the real HTTP client path without binding a port. Online cases
need real network; failures are recorded as environment problems, not regressions.

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


def _clear_probe_caches() -> None:
    for mod, names in (
        ("libref.services.code.probe", ("_PROBE_CACHE",)),
        ("libref.services.docs.probe", ("_DOC_PROBE_CACHE",)),
        ("libref.crawler.url_seeder", ("_SEED_CACHE",)),
    ):
        try:
            holder = __import__(mod, fromlist=names)
        except ImportError:
            continue
        for name in names:
            cache = getattr(holder, name, None)
            if isinstance(cache, dict):
                cache.clear()


def _run_local_cli(argv: list[str], db_path: Path) -> tuple[int, str, str, float]:
    from libref.cli import main

    old = os.environ.get("LIBREF_DB_PATH")
    os.environ["LIBREF_DB_PATH"] = str(db_path)
    _clear_probe_caches()
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


def _build_web_transport(db_path: Path):
    import httpx
    from fastapi.testclient import TestClient

    from libref.config import LibrefSettings
    from libref.db import apply_migrations, open_db
    from libref.web import build_web_app

    settings = LibrefSettings(auth_mode="dev", db_path=db_path)
    conn = open_db(db_path, check_same_thread=False)
    apply_migrations(conn)
    app = build_web_app(conn, settings)
    test_client = TestClient(app)

    def handler(request: httpx.Request) -> httpx.Response:
        response = test_client.request(
            request.method,
            request.url.path,
            params=dict(request.url.params),
            content=request.content,
            headers={"content-type": request.headers.get("content-type", "")},
        )
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"content-type": response.headers.get("content-type", "")},
            request=request,
        )

    return conn, test_client, httpx.MockTransport(handler)


def _run_http_cli(argv: list[str], transport) -> tuple[int, str, str, float]:

    from librefctl.app import build_parser, dispatch
    from librefctl.output import emit, exit_code, failure, success
    from librefctl.transport import Client, ClientError

    args = build_parser().parse_args(["--base-url", "http://web", *argv])
    client = Client("http://web", transport=transport)
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    started = time.time()
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            try:
                data, meta = dispatch(client, args)
                emit(success(data, meta))
                code = 0
            except ClientError as exc:
                emit(failure(exc))
                code = exit_code(exc.code)
    finally:
        client.close()
    return code, out_buf.getvalue(), err_buf.getvalue(), time.time() - started


def _write(
    out_dir: Path, case_id: str, label: str, argv: list[str], result: tuple
) -> None:
    code, stdout, stderr, elapsed = result
    (out_dir / f"{case_id}.stdout.txt").write_text(stdout, encoding="utf-8")
    (out_dir / f"{case_id}.meta.txt").write_text(
        "\n".join(
            [
                f"argv: {label} {' '.join(argv)}",
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


def _envelope(payload: str):
    data = json.loads(payload)
    if not isinstance(data, dict) or not data.get("ok"):
        return None
    return data.get("data")


def _summarize_probe_docs(payload: str) -> str:
    data = _envelope(payload)
    if data is None:
        return "unparsed"
    return (
        f"source={data.get('source')} "
        f"total={data.get('total_discovered')} "
        f"accepted={data.get('accepted')} "
        f"samples={len(data.get('samples', []))} "
        f"errors={len(data.get('errors', {}))}"
    )


def _summarize_discovery(payload: str) -> str:
    data = _envelope(payload)
    if not isinstance(data, dict):
        return f"unparsed stdout_head={payload[:80]!r}"
    items = data.get("items") or []
    first = items[0] if items else {}
    return f"candidates={len(items)} first_keys={sorted(first.keys())}"


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
        web_conn, web_client, web_transport = _build_web_transport(db_path)
        try:
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
                argv = ["local", "probe-repo", str(root)]
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
                result = _run_local_cli(argv, db_path)
                _write(out_dir, entry["id"], "libref", argv, result)
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
                    argv = ["doc-probe", str(bench_entry["start_url"])]
                    argv += [
                        "--sample-limit",
                        str(entry.get("sample_limit", 10)),
                        "--fetch-sample-limit",
                        str(entry.get("fetch_sample_limit", 3)),
                    ]
                    prefixes = bench_entry.get("path_prefixes") or []
                    if prefixes:
                        argv += ["--scope", json.dumps({"path_prefixes": prefixes})]
                    if bench_entry.get("max_pages"):
                        argv += ["--max-pages", str(bench_entry["max_pages"])]
                    result = _run_http_cli(argv, web_transport)
                    _write(out_dir, entry["id"], "librefctl", argv, result)
                    lines.append(
                        f"{entry['id']} bench={entry['bench']} exit={result[0]} "
                        + _summarize_probe_docs(result[1])
                    )
                for entry in cases.get("discovery", []):
                    if wanted and entry["id"] not in wanted:
                        continue
                    argv = list(entry["argv"])
                    result = _run_http_cli(argv, web_transport)
                    _write(out_dir, entry["id"], "librefctl", argv, result)
                    lines.append(
                        f"{entry['id']} exit={result[0]} "
                        + _summarize_discovery(result[1])
                    )
        finally:
            web_client.close()
            web_conn.close()
    (out_dir / "SUMMARY.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
