"""Versioned migrations: ordered version numbers plus an applied-versions record plus
single-transaction execution; idempotent replay, full rollback on failure."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from libref.db.schema import (
    AST_CHUNK_RELATION_SQL,
    CALL_RESOLVE_SQL,
    CALL_SCOPE_SQL,
    DISCOVERY_CACHE_SQL,
    FEATURE_TREE_CLUSTER_SQL,
    GOVERNANCE_SQL,
    IMPORT_EDGES_SQL,
    INITIAL_SCHEMA_SQL,
    MINING_PROVENANCE_INDEX_SQL,
    MINING_REVIEW_SCHEMA_SQL,
    PAGES_CONTENT_HASH_SQL,
    PATTERN_LABEL_SQL,
    PROGRESS_PENDING_SQL,
    PROGRESS_SQL,
    PROGRESS_VECTOR_WATERMARK_SQL,
    QUERY_SUPPORT_SQL,
    REPO_DEPS_SQL,
    REPOS_ASTCACHE_SQL,
    SYMBOL_DETAIL_SQL,
    VEC_VERSION_META_SQL,
    VECTOR_META_SQL,
    VERSION_SITE_EVIDENCE_SQL,
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(0, "initial-schema", INITIAL_SCHEMA_SQL),
    Migration(1, "mining-review-schema", MINING_REVIEW_SCHEMA_SQL),
    Migration(2, "vector-meta", VECTOR_META_SQL),
    Migration(3, "pages-content-hash", PAGES_CONTENT_HASH_SQL),
    Migration(4, "repos-astcache-call-scope", REPOS_ASTCACHE_SQL),
    Migration(5, "ast-chunk-relation", AST_CHUNK_RELATION_SQL),
    Migration(6, "mining-provenance-index", MINING_PROVENANCE_INDEX_SQL),
    Migration(7, "feature-tree-cluster", FEATURE_TREE_CLUSTER_SQL),
    Migration(8, "governance-tables", GOVERNANCE_SQL),
    Migration(9, "vec-version-meta", VEC_VERSION_META_SQL),
    Migration(10, "pattern-labels", PATTERN_LABEL_SQL),
    Migration(11, "query-support", QUERY_SUPPORT_SQL),
    Migration(12, "call-scope", CALL_SCOPE_SQL),
    Migration(13, "discovery-cache", DISCOVERY_CACHE_SQL),
    Migration(14, "repo-dependencies", REPO_DEPS_SQL),
    Migration(15, "ingest-progress", PROGRESS_SQL),
    Migration(16, "ingest-progress-vector-watermark", PROGRESS_VECTOR_WATERMARK_SQL),
    Migration(17, "symbol-detail", SYMBOL_DETAIL_SQL),
    Migration(18, "call-resolve", CALL_RESOLVE_SQL),
    Migration(19, "import-edges", IMPORT_EDGES_SQL),
    Migration(20, "version-site-evidence", VERSION_SITE_EVIDENCE_SQL),
    Migration(21, "ingest-progress-pending", PROGRESS_PENDING_SQL),
)

_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations(
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Set of applied migration version numbers (empty when not initialized)."""
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not row or row[0] == 0:
        return set()
    return {v for (v,) in conn.execute("SELECT version FROM schema_migrations")}


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order and return the versions applied this run.

    Idempotent: repeated calls are no-ops. A single failure rolls everything back
    and the version is not recorded.
    """
    conn.execute(_MIGRATIONS_TABLE_SQL)
    done = applied_versions(conn)
    newly_applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version in done:
            continue
        try:
            conn.execute("BEGIN")
            conn.executescript(migration.sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, name) VALUES(?, ?)",
                (migration.version, migration.name),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        newly_applied.append(migration.version)
    return newly_applied
