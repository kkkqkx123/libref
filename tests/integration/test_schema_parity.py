"""Schema parity: the PostgreSQL merge script must project the SQLite truth.

``db/schema.py`` plus ``db/migrations.py`` is the single source of truth; the
PostgreSQL consolidated DDL is its projection for fresh deployments. This
test fails on drift: same tables, same columns per table, same tenant flags,
same uniqueness on the idempotency keys. Intentional divergences (FTS
shadow tables, the PG tsvector column) live in the allowlists below, so a
new divergence cannot slip in silently.
"""

from __future__ import annotations

import os

import pytest

from libref.db.assembly import build_metadata_session
from libref.db.contract import DbIntegrityError

_PG_URL = os.environ.get("LIBREF_TEST_POSTGRES_URL", "")

needs_pg = pytest.mark.skipif(not _PG_URL, reason="LIBREF_TEST_POSTGRES_URL not set")

SQLITE_ONLY_TABLES = frozenset(
    {
        "documents_fts",
        "documents_fts_data",
        "documents_fts_idx",
        "documents_fts_content",
        "documents_fts_docsize",
        "documents_fts_segments",
        "documents_fts_segdir",
        "documents_fts_config",
    }
)

PG_ONLY_COLUMNS = {("documents", "content_tsv")}


class _SqliteSettings:
    metadata_backend = "sqlite"
    fulltext_backend = "fts5"
    db_path = ""


class _PostgresSettings:
    metadata_backend = "postgres"
    fulltext_backend = "postgres"
    postgres_url = _PG_URL
    postgres_pool_size = 2
    postgres_timeout = 5.0


def _fresh_postgres(url: str) -> None:
    import psycopg

    parsed = psycopg.conninfo.conninfo_to_dict(url)
    dbname = (parsed.get("dbname") or "postgres").replace('"', "")
    admin = dict(parsed, dbname="postgres")
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{dbname}"')


def _sqlite_tables(session) -> set[str]:
    rows = session.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(r[0]) for r in rows}


def _pg_tables(session) -> set[str]:
    rows = session.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname='public'"
    ).fetchall()
    return {str(r[0]) for r in rows}


@needs_pg
def test_schema_parity_sqlite_vs_postgres(tmp_path) -> None:
    sqlite_settings = _SqliteSettings()
    sqlite_settings.db_path = str(tmp_path / "parity.db")
    sqlite_session = build_metadata_session(sqlite_settings)
    sqlite_session.apply_migrations()
    _fresh_postgres(_PG_URL)
    pg_session = build_metadata_session(_PostgresSettings())
    pg_session.apply_migrations()
    try:
        sqlite_tables = _sqlite_tables(sqlite_session) - SQLITE_ONLY_TABLES
        pg_tables = _pg_tables(pg_session)
        assert sqlite_tables == pg_tables, sorted(sqlite_tables ^ pg_tables)

        for table in sorted(sqlite_tables):
            s_cols = set(sqlite_session.table_columns(table))
            p_cols = set(pg_session.table_columns(table))
            allowed = {col for (t, col) in PG_ONLY_COLUMNS if t == table}
            assert s_cols - p_cols == set(), (table, sorted(s_cols - p_cols))
            assert p_cols - s_cols == allowed, (table, sorted(p_cols - s_cols))
            assert sqlite_session.table_has_tenant(table) == (
                pg_session.table_has_tenant(table)
            ), table

        # Uniqueness on the idempotency keys holds on both branches.
        for session in (sqlite_session, pg_session):
            lib = session.insert(
                "INSERT INTO libraries(ecosystem, name) VALUES(?, ?)",
                ("npm", "parity-lib"),
            )
            ver = session.insert(
                "INSERT INTO versions(library_id, version) VALUES(?, ?)",
                (lib, "1.0.0"),
            )
            page = session.insert(
                "INSERT INTO pages(version_id, url) VALUES(?, ?)",
                (ver, "https://example.com/p"),
            )
            session.insert(
                "INSERT INTO documents(library_id, version_id, page_id, url,"
                " content, metadata, sort_order) VALUES(?,?,?,?,?,?,?)",
                (lib, ver, page, "https://example.com/p", "body", "{}", 0),
            )
            with pytest.raises(DbIntegrityError):
                session.insert(
                    "INSERT INTO documents(library_id, version_id, page_id, url,"
                    " content, metadata, sort_order) VALUES(?,?,?,?,?,?,?)",
                    (lib, ver, page, "https://example.com/p", "body", "{}", 0),
                )
    finally:
        sqlite_session.close()
        pg_session.close()
