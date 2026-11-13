"""Build the retrieval benchmark dataset from downloaded fixtures.

`fetch.py` performs the real acquisition: it crawls the documentation site with
the production ingest service and clones the source repository with the
production repository manager, leaving raw page bodies and the clone under the
gitignored `fixtures/` directory. This script replays those downloads offline
through the production cleaning, chunking, indexing and mining paths, so a
rebuild never touches the network and always describes the same download.

Usage:
    uv run python -m tests.e2e.bench.build [--out DIR] [--fixture NAME]...

Missing fixtures abort the build with the command that produces them: a
half-built dataset would silently change what the numbers mean.
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = HERE / "bench.json"
FIXTURES = HERE / "fixtures"
PAGE_CACHE = FIXTURES / "docs" / "pages.jsonl.gz"
LOCK = FIXTURES / "lock.json"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="build the retrieval benchmark dataset"
    )
    parser.add_argument("--out", default=str(HERE / "data"))
    parser.add_argument(
        "--fixture",
        action="append",
        default=None,
        help="fixture name from bench.json; defaults to all",
    )
    return parser.parse_args()


def load_spec(path: Path = SPEC) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_lock() -> dict:
    if not LOCK.is_file():
        raise SystemExit(
            "benchmark fixtures missing: run `uv run python -m tests.e2e.bench.fetch` first"
        )
    return json.loads(LOCK.read_text(encoding="utf-8"))


def _cached_pages() -> list[dict]:
    if not PAGE_CACHE.is_file():
        raise SystemExit(f"page cache missing: {PAGE_CACHE}; rerun the fetch step")
    with gzip.open(PAGE_CACHE, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _index_docs(conn, settings, entry: dict, lock: dict) -> dict:
    """Replay the crawled pages through the production clean, split and store path."""
    from libref.chunking import split_markdown_dual
    from libref.services.docs.page_pipeline import PagePipeline
    from libref.services.docs.scrape_plan import build_doc_inputs, doc_budgets
    from libref.services.registry.libraries import LibraryService
    from libref.store.document_store import DocumentStore
    from libref.text.normalize import classify_content

    library = str(entry["library"])
    version = str(entry["version"])
    ecosystem = str(entry.get("ecosystem") or "custom")
    library_id, version_id = LibraryService(conn).ensure_library_version(
        library, version, ecosystem
    )
    budgets = doc_budgets(settings)
    pipeline = PagePipeline(settings)
    store = DocumentStore(conn)
    lanes = {"embedding": 0, "bm25": 0}
    pages = 0
    chunks = 0
    empty = 0
    for row in _cached_pages():
        url = row["url"]
        content_type = row["content_type"]
        markdown = pipeline.clean_with_cache(
            classify_content(content_type, url), row["body"], url
        )
        if not markdown.strip():
            empty += 1
            continue
        dual = split_markdown_dual(markdown, url, budgets=budgets)
        inputs = build_doc_inputs(
            dual.embedding + dual.bm25, "text/markdown", "markdown"
        )
        if not inputs:
            empty += 1
            continue
        store.store_page(
            version_id,
            library_id,
            url,
            "stored",
            chunks=inputs,
            raw_content=markdown,
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
        "ecosystem": ecosystem,
        "library_id": library_id,
        "version_id": version_id,
        "source": lock.get("start_url", ""),
        "urls_sha256": lock.get("urls_sha256", ""),
        "fetched_at": lock.get("fetched_at", ""),
        "pages": pages,
        "empty": empty,
        "chunks": chunks,
        "lanes": lanes,
    }


def _index_code(conn, settings, entry: dict, lock: dict, out_dir: Path) -> dict:
    """Index the cloned repository through the production parse, chunk and mine path."""
    from libref.services.code.code_ingest import index_repo
    from libref.services.code.mining_refresh import mine_patterns
    from libref.services.registry.libraries import LibraryService

    source = Path(str(lock["path"]))
    subdir = str(entry.get("subdir") or "")
    if not (source / subdir).is_dir():
        raise SystemExit(f"cloned repository missing {subdir}: {source}")
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
    library_id, version_id = LibraryService(conn).ensure_library_version(
        library, version, ecosystem
    )
    patterns_total = conn.execute(
        "SELECT count(*) FROM seq_patterns WHERE version_id=?", (version_id,)
    ).fetchone()
    clusters = conn.execute(
        "SELECT count(*) FROM api_clusters WHERE version_id=?", (version_id,)
    ).fetchone()
    return {
        "kind": "code",
        "library": library,
        "version": version,
        "ecosystem": ecosystem,
        "library_id": library_id,
        "version_id": version_id,
        "source": lock.get("repo_url", ""),
        "commit": lock.get("commit", ""),
        "subdir": subdir,
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
    """Rebuild the dataset from the downloaded fixtures and write its manifest.

    Settings overrides exist for indexing-parameter sweeps: chunk budgets are
    fixed at build time, so a variant needs its own dataset.
    """
    from libref.db import apply_migrations, open_db
    from tests.e2e.bench.fakes import bench_settings

    spec = load_spec()
    lock = load_lock()
    wanted = names or list(spec)
    for name in wanted:
        if name not in lock:
            raise SystemExit(
                f"fixture '{name}' not downloaded: "
                "run `uv run python -m tests.e2e.bench.fetch` first"
            )
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "bench.db"
    settings = bench_settings(db_path, **(settings_overrides or {}))
    conn = open_db(db_path)
    apply_migrations(conn)
    manifest: dict = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fixtures": {},
    }
    try:
        for name in wanted:
            entry = spec[name]
            started = time.monotonic()
            if entry["kind"] == "docs":
                info = _index_docs(conn, settings, entry, lock[name])
            else:
                info = _index_code(conn, settings, entry, lock[name], out_dir)
            info["elapsed_ms"] = int((time.monotonic() - started) * 1000)
            manifest["fixtures"][name] = info
    finally:
        conn.close()
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    args = _parse_args()
    manifest = build(Path(args.out), args.fixture)
    for name, info in manifest["fixtures"].items():
        summary = ", ".join(f"{key}={value}" for key, value in info.items())
        print(f"{name}: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
