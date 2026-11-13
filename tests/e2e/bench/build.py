"""Build the retrieval benchmark dataset from the frozen fixture archive.

`fetch.py` performs the one-time acquisition and freezes the result under
`fixtures/{project}/{kind}`; this script verifies that archive (identity,
payload digest, and the content digest of the pages or source tree) and replays
it offline through the production cleaning, chunking, indexing and mining paths.
A rebuild never touches the network, and a modified archive is a hard failure
rather than a silently different corpus.

Usage:
    uv run python -m tests.e2e.bench.build [--out DIR] [--fixture ID]...
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from tests.e2e.bench import archive as bench_archive

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="build the retrieval benchmark dataset"
    )
    parser.add_argument("--out", default=str(DATA))
    parser.add_argument(
        "--fixture",
        action="append",
        default=None,
        help="fixture id such as httpx/docs; defaults to every archived fixture",
    )
    return parser.parse_args()


DATASET_MEMBERS = frozenset({"bench.db", "manifest.json"})


def _prepare_out(out_dir: Path) -> None:
    """Reuse only an empty directory or a previous dataset, never an arbitrary one."""
    if not out_dir.exists():
        return
    extra = {path.name for path in out_dir.iterdir()} - DATASET_MEMBERS
    if extra:
        raise bench_archive.ArchiveError(
            f"refusing to rebuild {out_dir}: it holds {sorted(extra)}; point --out at "
            "a dataset directory or remove it yourself"
        )
    shutil.rmtree(out_dir)


def _verify_pages(entry: dict[str, Any], meta: dict[str, Any]) -> Path:
    payload = bench_archive.payload_path(entry, meta)
    pages, urls = bench_archive.pages_content(payload)
    if pages != meta["content"]["pages"] or urls != meta["content"]["urls_sha256"]:
        raise bench_archive.ArchiveError(
            f"fixture {bench_archive.fixture_id(entry)}: page payload does not match "
            "the archived content digest"
        )
    return payload


def _source(entry: dict[str, Any], meta: dict[str, Any], parent: Path) -> Path:
    payload = bench_archive.payload_path(entry, meta)
    root = bench_archive.extract_source(payload, parent / entry["project"])
    digest, files = bench_archive.tree_digest(root)
    if digest != meta["content"]["tree_sha256"] or files != meta["content"]["files"]:
        raise bench_archive.ArchiveError(
            f"fixture {bench_archive.fixture_id(entry)}: source payload does not "
            "match the archived content digest"
        )
    return root


def _index_docs(conn, settings, entry: dict[str, Any], meta: dict[str, Any]) -> dict:
    """Replay the archived page bodies through the production clean, split and store path."""
    from libref.chunking import split_markdown_dual
    from libref.services.docs.page_pipeline import PagePipeline
    from libref.services.docs.scrape_plan import build_doc_inputs, doc_budgets
    from libref.services.registry.libraries import LibraryService
    from libref.store.document_store import DocumentStore
    from libref.text.normalize import classify_content

    library = str(entry["library"])
    version = str(entry["version"])
    ecosystem = str(entry["ecosystem"])
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
    for row in bench_archive.iter_pages(_verify_pages(entry, meta)):
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
        "pages": pages,
        "empty": empty,
        "chunks": chunks,
        "lanes": lanes,
        "urls_sha256": meta["content"]["urls_sha256"],
    }


def _index_code(conn, settings, entry: dict[str, Any], meta: dict[str, Any]) -> dict:
    """Index the frozen checkout through the production parse, chunk and mine path.

    The tree is unpacked into a temporary directory: third-party sources never
    land inside the repository, where they would be collected by tooling and
    would add a second copy of the archive on disk.
    """
    from libref.services.code.code_ingest import index_repo
    from libref.services.code.mining_refresh import mine_patterns
    from libref.services.registry.libraries import LibraryService

    with tempfile.TemporaryDirectory(prefix="libref-bench-src-") as tmp:
        source = _source(entry, meta, Path(tmp))
        subdir = str(meta["content"].get("subdir") or "")
        if subdir and not (source / subdir).is_dir():
            raise bench_archive.ArchiveError(
                f"fixture {bench_archive.fixture_id(entry)}: archived tree lacks {subdir}"
            )
        counts = index_repo(
            conn,
            str(entry["library"]),
            str(entry["version"]),
            str(source),
            repos_dir=Path(tmp),
            ecosystem=str(entry["ecosystem"]),
            settings=settings,
            subdir=subdir or None,
        )
    library = str(entry["library"])
    version = str(entry["version"])
    ecosystem = str(entry["ecosystem"])
    patterns = mine_patterns(conn, library, version, ecosystem=ecosystem)
    _library_id, version_id = LibraryService(conn).ensure_library_version(
        library, version, ecosystem
    )
    patterns_total = conn.execute(
        "SELECT count(*) FROM seq_patterns WHERE version_id=?", (version_id,)
    ).fetchone()
    clusters = conn.execute(
        "SELECT count(*) FROM api_clusters WHERE version_id=?", (version_id,)
    ).fetchone()
    return {
        "files": int(counts.get("files", 0)),
        "parsed": int(counts.get("parsed", 0)),
        "entities": int(counts.get("entities", 0)),
        "calls": int(counts.get("calls", 0)),
        "source_chunks": int(counts.get("source_chunks", 0)),
        "patterns": int(patterns_total[0]) if patterns_total else 0,
        "pattern_hits": len(patterns),
        "clusters": int(clusters[0]) if clusters else 0,
        "commit": str(meta["content"].get("commit") or ""),
    }


def build(
    out_dir: Path = DATA,
    names: list[str] | None = None,
    settings_overrides: dict | None = None,
) -> dict:
    """Rebuild the dataset from the archive and write its manifest.

    Settings overrides exist for indexing-parameter sweeps: chunk budgets are
    fixed at build time, so a variant needs its own dataset.
    """
    from libref.db import apply_migrations, open_db
    from tests.e2e.bench.fakes import bench_settings

    entries = bench_archive.select(names)
    verified = {bench_archive.fixture_id(e): bench_archive.verify(e) for e in entries}
    _prepare_out(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "bench.db"
    settings = bench_settings(db_path, **(settings_overrides or {}))
    conn = open_db(db_path)
    apply_migrations(conn)
    manifest: dict[str, Any] = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fixtures": {},
    }
    try:
        for entry in entries:
            name = bench_archive.fixture_id(entry)
            meta = verified[name]
            started = time.monotonic()
            if entry["kind"] == "docs":
                info = _index_docs(conn, settings, entry, meta)
            else:
                info = _index_code(conn, settings, entry, meta)
            info.update(
                {
                    "kind": entry["kind"],
                    "project": entry["project"],
                    "library": entry["library"],
                    "version": entry["version"],
                    "ecosystem": entry["ecosystem"],
                    "revision": meta["revision"],
                    "payload_sha256": meta["payload"]["sha256"],
                    "acquired_at": meta["acquired_at"],
                    "source": meta["recipe"].get(
                        "start_url" if entry["kind"] == "docs" else "repo_url", ""
                    ),
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
            )
            manifest["fixtures"][name] = info
    finally:
        conn.close()
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    args = _parse_args()
    try:
        manifest = build(Path(args.out), args.fixture)
    except bench_archive.ArchiveError as exc:
        print(f"FAIL {exc}")
        return 1
    for name, info in manifest["fixtures"].items():
        summary = ", ".join(f"{key}={value}" for key, value in info.items())
        print(f"{name}: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
