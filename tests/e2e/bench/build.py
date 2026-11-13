"""Build the retrieval benchmark dataset from real project fixtures.

The corpus is the repository itself: its documentation tree feeds the document
branch and its own source tree feeds the code branch, both through the
production pipelines. The dataset lives outside version control and is rebuilt
from the fixtures on demand, so measurements always describe current code.

Usage:
    uv run python tests/e2e/bench/build.py [--out DIR] [--fixture NAME]...

A fixture whose path is missing is reported and skipped; the run then marks the
environment unavailable instead of failing as a regression.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
SPEC = HERE / "fixtures.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="build the retrieval benchmark dataset"
    )
    parser.add_argument("--out", default=str(HERE / "data"))
    parser.add_argument(
        "--fixture",
        action="append",
        default=None,
        help="fixture name from fixtures.json; defaults to all",
    )
    return parser.parse_args()


def load_spec(path: Path = SPEC) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["fixtures"]


def _index_docs(conn, settings, name: str, entry: dict) -> dict:
    """Index a markdown tree through the production cleaning and dual-path chunking."""
    from libref.chunking import split_markdown_dual
    from libref.services.docs.scrape_plan import (
        build_doc_inputs,
        doc_budgets,
        normalize_text_content,
    )
    from libref.services.registry.libraries import LibraryService
    from libref.store.document_store import DocumentStore

    root = (ROOT / entry["path"]).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"fixture '{name}' path missing: {root}")
    library = str(entry["library"])
    version = str(entry["version"])
    library_id, version_id = LibraryService(conn).ensure_library_version(
        library, version, "custom"
    )
    budgets = doc_budgets(settings)
    store = DocumentStore(conn)
    url_base = str(entry.get("url_base") or "https://bench.local").rstrip("/")
    pages = 0
    chunks = 0
    lanes = {"embedding": 0, "bm25": 0}
    patterns = list(entry.get("include") or ["**/*.md"])
    files = sorted({path for pattern in patterns for path in root.glob(pattern)})
    for path in files:
        rel = path.relative_to(root).as_posix()
        url = f"{url_base}/{entry['path']}/{rel}"
        content = normalize_text_content(path.read_text(encoding="utf-8"))
        if not content.strip():
            continue
        dual = split_markdown_dual(content, url, budgets=budgets)
        inputs = build_doc_inputs(
            dual.embedding + dual.bm25, "text/markdown", "markdown"
        )
        if not inputs:
            continue
        store.store_page(
            version_id,
            library_id,
            url,
            "stored",
            chunks=inputs,
            raw_content=content,
        )
        pages += 1
        chunks += len(inputs)
        for item in inputs:
            lanes[item.chunk_path] = lanes.get(item.chunk_path, 0) + 1
    conn.commit()
    return {
        "kind": "docs",
        "library": library,
        "version": version,
        "library_id": library_id,
        "version_id": version_id,
        "root": str(root),
        "pages": pages,
        "chunks": chunks,
        "lanes": lanes,
    }


def _index_code(conn, settings, name: str, entry: dict, out_dir: Path) -> dict:
    """Index a source tree through the production clone/parse/chunk pipeline."""
    from libref.services.code.code_ingest import index_repo
    from libref.services.code.mining_refresh import mine_patterns
    from libref.services.registry.libraries import LibraryService

    source = (ROOT / str(entry.get("source") or ".")).resolve()
    subdir = str(entry.get("subdir") or "")
    if not (source / subdir).is_dir():
        raise FileNotFoundError(f"fixture '{name}' path missing: {source / subdir}")
    library = str(entry["library"])
    version = str(entry["version"])
    ecosystem = str(entry.get("ecosystem") or "custom")
    counts = index_repo(
        conn,
        library,
        version,
        str(source),
        repos_dir=out_dir / "repos",
        ecosystem=ecosystem,
        settings=settings,
        subdir=subdir or None,
    )
    patterns = mine_patterns(conn, library, version, ecosystem=ecosystem)
    catalog = LibraryService(conn)
    library_id, version_id = catalog.ensure_library_version(library, version, ecosystem)
    patterns_total = conn.execute(
        "SELECT count(*) FROM seq_patterns WHERE version_id=?", (version_id,)
    ).fetchone()
    clusters = conn.execute(
        "SELECT count(*) FROM api_clusters WHERE version_id=?", (version_id,)
    ).fetchone()
    repo = conn.execute(
        "SELECT commit_sha, url FROM repos WHERE name=?",
        (f"{ecosystem}/{library}/{version}",),
    ).fetchone()
    return {
        "kind": "code",
        "library": library,
        "version": version,
        "library_id": library_id,
        "version_id": version_id,
        "root": str(source),
        "subdir": subdir,
        "commit": repo[0] if repo else "",
        "source": repo[1] if repo else "",
        "files": int(counts.get("files", 0)),
        "parsed": int(counts.get("parsed", 0)),
        "entities": int(counts.get("entities", 0)),
        "calls": int(counts.get("calls", 0)),
        "source_chunks": int(counts.get("source_chunks", 0)),
        "patterns": int(patterns_total[0]) if patterns_total else 0,
        "pattern_hits": len(patterns),
        "clusters": int(clusters[0]) if clusters else 0,
    }


def build(
    out_dir: Path = HERE / "data",
    names: list[str] | None = None,
    settings_overrides: dict | None = None,
) -> dict:
    """Rebuild the whole dataset from the fixtures and write its manifest.

    Settings overrides exist for indexing-parameter sweeps: chunk budgets are
    fixed at build time, so a variant needs its own dataset.
    """
    from libref.db import apply_migrations, open_db
    from tests.e2e.bench.fakes import bench_settings

    spec = load_spec()
    wanted = names or list(spec)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "bench.db"
    settings = bench_settings(db_path, **(settings_overrides or {}))
    conn = open_db(db_path)
    apply_migrations(conn)
    manifest: dict = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "root": str(ROOT),
        "commit": _head_commit(),
        "fixtures": {},
        "skipped": [],
    }
    try:
        for name in wanted:
            entry = spec[name]
            started = time.monotonic()
            if entry["kind"] == "docs":
                info = _index_docs(conn, settings, name, entry)
            else:
                info = _index_code(conn, settings, name, entry, out_dir)
            info["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            manifest["fixtures"][name] = info
    except FileNotFoundError as exc:
        manifest["skipped"].append(str(exc))
    finally:
        conn.close()
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _head_commit() -> str:
    from dulwich import porcelain

    try:
        head = porcelain.revparse_single(str(ROOT), "HEAD")
    except Exception:  # noqa: BLE001 -- absence of git metadata must not block the build
        return ""
    return getattr(head, "id", b"").decode("ascii", "ignore")


def main() -> int:
    args = _parse_args()
    manifest = build(Path(args.out), args.fixture)
    for name, info in manifest["fixtures"].items():
        summary = ", ".join(f"{k}={v}" for k, v in info.items() if k != "root")
        print(f"{name}: {summary}")
    for reason in manifest["skipped"]:
        print(f"SKIP: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
