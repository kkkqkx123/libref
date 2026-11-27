#!/usr/bin/env python3
"""Replay one benchmark fixture pair into an ignored graph-gold derived database.

The source benchmark database is copied, never modified. Document-side facts are
moved onto the bound source version inside the copy, mining profiles are merged,
and document navigation edges are extracted again from the frozen page bodies.
The result is a version-scoped read model for manual graph gold authoring, not a
second production truth and not a gold generator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DOC_EXCLUSIVE_TABLES = (
    "doc_api_links",
    "doc_consistency",
    "doc_feature_node_links",
    "doc_features",
    "doc_pattern_coverage",
    "seq_patterns",
    "api_clusters",
    "feature_nodes",
    "feature_facets",
    "cluster_labels",
    "pattern_labels",
    "doc_snippet_records",
    "doc_snippet_sources",
    "doc_link_edges",
)
MIXED_TABLES = ("documents", "pages", "call_instances")
PROFILE_TABLES = (
    "doc_consistency",
    "doc_feature_node_links",
    "doc_pattern_coverage",
    "seq_patterns",
    "api_clusters",
    "feature_nodes",
    "feature_facets",
    "cluster_labels",
    "pattern_labels",
)
DATASET_MEMBERS = frozenset({"graph-gold.db", "graph-gold.manifest.json"})
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def _prepare_out(out_dir: Path) -> None:
    if not out_dir.exists():
        return
    extra = {path.name for path in out_dir.iterdir()} - DATASET_MEMBERS
    if extra:
        raise ValueError(
            f"refusing to rebuild {out_dir}: it holds {sorted(extra)}; use an empty "
            "directory or a previous graph-gold dataset"
        )
    shutil.rmtree(out_dir)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _has_column(conn, table: str, column: str) -> bool:
    return any(
        str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})")
    )


def _binding(
    conn, library: str, doc_version: str, tenant_id: str
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT b.doc_version_id, b.source_version_id, dv.version, sv.version,"
        " l.ecosystem FROM entity_source_bindings b"
        " JOIN versions dv ON dv.id=b.doc_version_id"
        " JOIN versions sv ON sv.id=b.source_version_id"
        " JOIN libraries l ON l.id=dv.library_id"
        " WHERE l.name=? AND dv.version=? AND b.tenant_id=?",
        (library, doc_version, tenant_id),
    ).fetchone()
    if row is None:
        raise ValueError(f"no entity source binding for {library} {doc_version}")
    return {
        "doc_version_id": int(row[0]),
        "source_version_id": int(row[1]),
        "doc_version": str(row[2]),
        "source_version": str(row[3]),
        "ecosystem": str(row[4]),
    }


def _profile_map(
    conn, doc_version_id: int, source_version_id: int, tenant_id: str
) -> dict[int, int]:
    rows = conn.execute(
        "SELECT id, name FROM mining_profiles"
        " WHERE version_id=? AND tenant_id=? ORDER BY id",
        (doc_version_id, tenant_id),
    ).fetchall()
    mapping: dict[int, int] = {}
    for old_id, name in rows:
        other = conn.execute(
            "SELECT id FROM mining_profiles"
            " WHERE version_id=? AND name=? AND tenant_id=? LIMIT 1",
            (source_version_id, str(name), tenant_id),
        ).fetchone()
        if other is None:
            conn.execute(
                "UPDATE mining_profiles SET version_id=? WHERE id=?",
                (source_version_id, int(old_id)),
            )
            mapping[int(old_id)] = int(old_id)
            continue
        target = int(other[0])
        if target == int(old_id):
            continue
        for table in PROFILE_TABLES:
            if not _has_column(conn, table, "profile_id"):
                continue
            clash = conn.execute(
                f"SELECT 1 FROM {table} WHERE version_id=? AND profile_id=? LIMIT 1",
                (source_version_id, target),
            ).fetchone()
            if clash is not None:
                raise ValueError(
                    f"profile merge would collide in {table} for profile {name}"
                )
        mapping[int(old_id)] = target
    return mapping


def _remap_profiles(
    conn, mapping: dict[int, int], doc_version_id: int, tenant_id: str
) -> None:
    for old_id, new_id in sorted(mapping.items()):
        if old_id == new_id:
            continue
        for table in PROFILE_TABLES:
            params: list[Any] = [new_id, old_id, doc_version_id]
            sql = f"UPDATE {table} SET profile_id=? WHERE profile_id=? AND version_id=?"
            if _has_column(conn, table, "tenant_id"):
                sql += " AND tenant_id=?"
                params.append(tenant_id)
            conn.execute(sql, params)
        conn.execute(
            "DELETE FROM mining_profiles WHERE id=? AND tenant_id=?",
            (old_id, tenant_id),
        )


def _remap_versions(
    conn, doc_version_id: int, source_version_id: int, tenant_id: str
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in DOC_EXCLUSIVE_TABLES + MIXED_TABLES:
        if table in DOC_EXCLUSIVE_TABLES:
            existing = conn.execute(
                f"SELECT count(*) FROM {table} WHERE version_id=?",
                (source_version_id,),
            ).fetchone()[0]
            if existing:
                raise ValueError(f"{table} already has {existing} source-version rows")
        params: list[Any] = [source_version_id, doc_version_id]
        sql = f"UPDATE {table} SET version_id=? WHERE version_id=?"
        if _has_column(conn, table, "tenant_id"):
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        cursor = conn.execute(sql, params)
        counts[table] = int(cursor.rowcount or 0)
    return counts


def _markdown_links(raw: str) -> list[str]:
    """Collect markdown links outside fenced code, in document order."""
    links: list[str] = []
    fence = ""
    for line in raw.splitlines():
        marker = _FENCE.match(line)
        if marker is not None:
            token = marker.group(1)
            if not fence:
                fence = token[0]
            elif token[0] == fence:
                fence = ""
            continue
        if fence:
            continue
        clean = re.sub(r"`[^`]*`", "", line)
        for match in _MARKDOWN_LINK.finditer(clean):
            if not match.group(0).startswith("!"):
                links.append(match.group(1).strip("<>"))
    return links


def _extract_links(
    conn, library: str, doc_version: str, source_version_id: int, tenant_id: str
) -> int:
    from libref.crawler.scope import crawl_key
    from libref.services.docs.links import replace_version_edges
    from libref.services.docs.page_pipeline import PagePipeline
    from libref.text.normalize import classify_content
    from tests.e2e.bench import archive as bench_archive
    from tests.e2e.bench.fakes import bench_settings

    entry = next(
        (
            item
            for item in bench_archive.load_spec()
            if item["kind"] == "docs"
            and str(item["library"]) == library
            and str(item["version"]) == doc_version
        ),
        None,
    )
    if entry is None:
        raise ValueError(f"no archived docs fixture for {library} {doc_version}")
    meta = bench_archive.verify(entry)
    payload = bench_archive.payload_path(entry, meta)
    settings = bench_settings(ROOT / "tests/e2e/bench/data/graph-gold.db")
    pipeline = PagePipeline(settings)
    edges: dict[str, set[tuple[str, str]]] = {}
    for row in bench_archive.iter_pages(payload):
        url = str(row["url"])
        raw = str(row["body"])
        kind = classify_content(str(row["content_type"]), url)
        if kind == "html":
            scraped = pipeline.scrape_cached(raw, url)
            if scraped.nofollow:
                continue
            links = (
                scraped.internal_links
                + scraped.external_links
                + scraped.structure_links
            )
        else:
            links = _markdown_links(raw)
        dsts = edges.setdefault(url, set())
        host = urlsplit(url).netloc
        for href in links:
            target = urljoin(url, href)
            key = crawl_key(target)
            if not key:
                continue
            external = bool(urlsplit(target).netloc) and urlsplit(target).netloc != host
            dsts.add((key, "external" if external else "internal"))
    library_id = conn.execute(
        "SELECT id FROM libraries WHERE name=?", (library,)
    ).fetchone()[0]
    return int(replace_version_edges(conn, int(library_id), source_version_id, edges))


def build(
    *,
    source_db: Path,
    out_dir: Path,
    library: str,
    doc_version: str,
    tenant_id: str,
) -> dict[str, Any]:
    from libref.db import open_db

    if not source_db.is_file():
        raise ValueError(f"source database is missing: {source_db}")
    binding_source = _sha256(source_db)
    _prepare_out(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_db = out_dir / "graph-gold.db"
    shutil.copy2(source_db, out_db)
    conn = open_db(out_db)
    started = time.monotonic()
    try:
        binding = _binding(conn, library, doc_version, tenant_id)
        source_version_id = int(binding["source_version_id"])
        doc_version_id = int(binding["doc_version_id"])
        profile_map = _profile_map(conn, doc_version_id, source_version_id, tenant_id)
        _remap_profiles(conn, profile_map, doc_version_id, tenant_id)
        counts = _remap_versions(conn, doc_version_id, source_version_id, tenant_id)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_gold_documents_url"
            " ON documents(version_id, url)"
        )
        links = _extract_links(
            conn, library, doc_version, source_version_id, tenant_id
        )
        conn.execute(
            "DELETE FROM entity_source_bindings WHERE doc_version_id=?"
            " AND source_version_id=? AND tenant_id=?",
            (doc_version_id, source_version_id, tenant_id),
        )
        conn.commit()
    finally:
        conn.close()
    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_db": str(source_db),
        "source_db_sha256": binding_source,
        "library": library,
        "tenant_id": tenant_id,
        **binding,
        "profile_map": {str(k): v for k, v in sorted(profile_map.items())},
        "remapped_rows": counts,
        "doc_link_edges": links,
        "derived_indexes": ["idx_graph_gold_documents_url"],
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    (out_dir / "graph-gold.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-db", type=Path, default=ROOT / "tests/e2e/bench/data/bench.db"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "tests/e2e/bench/data/graph-gold",
    )
    parser.add_argument("--library", default="gradle")
    parser.add_argument("--doc-version", default="docs")
    parser.add_argument("--tenant-id", default="default")
    args = parser.parse_args()
    manifest = build(
        source_db=args.source_db.resolve(),
        out_dir=args.out_dir.resolve(),
        library=args.library,
        doc_version=args.doc_version,
        tenant_id=args.tenant_id,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
