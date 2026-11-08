"""版本化迁移：有序版本号加已应用记录加单条事务执行，幂等重放、失败整体回滚。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from libref.db.schema import (
    AST_CHUNK_RELATION_SQL,
    FEATURE_TREE_CLUSTER_SQL,
    INITIAL_SCHEMA_SQL,
    MINING_PROVENANCE_INDEX_SQL,
    MINING_REVIEW_SCHEMA_SQL,
    PAGES_CONTENT_HASH_SQL,
    REPOS_ASTCACHE_SQL,
    VECTOR_META_SQL,
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
)

_MIGRATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations(
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """已应用的迁移版本号集合（未初始化时为空）。"""
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not row or row[0] == 0:
        return set()
    return {v for (v,) in conn.execute("SELECT version FROM schema_migrations")}


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """按序应用未执行的迁移，返回本次应用的版本号列表。

    幂等：重复调用为 no-op。单条失败则整体回滚、不记录版本号。
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
