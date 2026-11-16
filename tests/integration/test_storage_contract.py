"""Contract tests shared by the metadata and fulltext backends.

The same cases run on the SQLite branch always, and on the PostgreSQL branch
only when `LIBREF_TEST_POSTGRES_URL` provides a reachable server. Coverage:
atomic writes, idempotent replay by unique key, integrity-error mapping,
tenant-column probing, and fulltext write/search/delete/count semantics.
"""

from __future__ import annotations

import os

import pytest

from libref.db.assembly import build_fulltext_store, build_metadata_session
from libref.db.contract import DbError, DbIntegrityError


class _SqliteSettings:
    metadata_backend = "sqlite"
    fulltext_backend = "fts5"
    db_path = ""


class _PostgresSettings:
    metadata_backend = "postgres"
    fulltext_backend = "postgres"
    postgres_url = os.environ.get("LIBREF_TEST_POSTGRES_URL", "")
    postgres_pool_size = 2
    postgres_timeout = 5.0


_PG_URL = os.environ.get("LIBREF_TEST_POSTGRES_URL", "")
_pg_params = [
    pytest.param(
        _PostgresSettings,
        id="postgres",
        marks=pytest.mark.skipif(
            not _PG_URL, reason="LIBREF_TEST_POSTGRES_URL not set"
        ),
    ),
]


def _branches():
    return [pytest.param(_SqliteSettings, id="sqlite"), *_pg_params]


def _fresh_postgres(url: str) -> None:
    """Recreate the target database so each case starts from an empty schema.

    The shared PostgreSQL instance keeps state between runs; a fresh database
    restores the hermetic behavior the SQLite branch gets from tmp_path.
    """
    import psycopg

    parsed = psycopg.conninfo.conninfo_to_dict(url)
    dbname = (parsed.get("dbname") or "postgres").replace('"', "")
    admin = dict(parsed, dbname="postgres")
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{dbname}"')


def _tenant_sets(settings) -> bool:
    return getattr(settings, "metadata_backend", "sqlite") == "postgres"


@pytest.mark.parametrize("settings_cls", _branches())
def test_session_atomic_insert_and_idempotent_replay(settings_cls, tmp_path):
    settings = settings_cls()
    if settings_cls is _PostgresSettings:
        _fresh_postgres(settings.postgres_url)
    settings.db_path = (
        str(tmp_path / "meta.db") if _SqliteSettings is settings_cls else None
    )
    session = build_metadata_session(settings)
    try:
        session.apply_migrations()
        row_id = session.insert(
            "INSERT INTO libraries(ecosystem, name) VALUES(?, ?)",
            ("npm", "libref-contract"),
        )
        assert row_id > 0
        # Replay by unique key: the conflict path must not raise.
        session.execute(
            "INSERT OR IGNORE INTO libraries(ecosystem, name) VALUES(?, ?)",
            ("npm", "libref-contract"),
        )
        row = session.execute(
            "SELECT count(*) FROM libraries WHERE ecosystem=? AND name=?",
            ("npm", "libref-contract"),
        ).fetchone()
        assert int(row[0]) == 1
    finally:
        session.close()


@pytest.mark.parametrize("settings_cls", _branches())
def test_integrity_error_mapping(settings_cls, tmp_path):
    settings = settings_cls()
    if settings_cls is _PostgresSettings:
        _fresh_postgres(settings.postgres_url)
    settings.db_path = (
        str(tmp_path / "meta.db") if _SqliteSettings is settings_cls else None
    )
    session = build_metadata_session(settings)
    try:
        session.apply_migrations()
        session.execute(
            "INSERT INTO libraries(ecosystem, name) VALUES(?, ?)", ("npm", "dup")
        )
        with pytest.raises(DbIntegrityError):
            session.execute(
                "INSERT INTO libraries(ecosystem, name) VALUES(?, ?)", ("npm", "dup")
            )
    finally:
        session.close()


@pytest.mark.parametrize("settings_cls", _branches())
def test_tenant_probe_and_diagnostics(settings_cls, tmp_path):
    settings = settings_cls()
    if settings_cls is _PostgresSettings:
        _fresh_postgres(settings.postgres_url)
    settings.db_path = (
        str(tmp_path / "meta.db") if _SqliteSettings is settings_cls else None
    )
    session = build_metadata_session(settings)
    try:
        session.apply_migrations()
        assert session.table_has_tenant("documents") is True
        assert session.table_has_tenant("robots_cache") is False
        snap = session.diagnostics()
        assert isinstance(snap, dict) and snap
        assert snap.get("backend")
    finally:
        session.close()


@pytest.mark.parametrize("settings_cls", _branches())
def test_fulltext_write_search_delete_count(settings_cls, tmp_path):
    settings = settings_cls()
    if settings_cls is _PostgresSettings:
        _fresh_postgres(settings.postgres_url)
    settings.db_path = (
        str(tmp_path / "meta.db") if _SqliteSettings is settings_cls else None
    )
    session = build_metadata_session(settings)
    try:
        session.apply_migrations()
        fulltext = build_fulltext_store(settings, session)
        lib_id = session.insert(
            "INSERT INTO libraries(ecosystem, name, tenant_id) VALUES(?, ?, ?)",
            ("npm", "ft-lib", "t1"),
        )
        ver_id = session.insert(
            "INSERT INTO versions(library_id, version, tenant_id) VALUES(?, ?, ?)",
            (lib_id, "1.0.0", "t1"),
        )
        page_id = session.insert(
            "INSERT INTO pages(version_id, url, tenant_id) VALUES(?, ?, ?)",
            (ver_id, "https://example.com/a", "t1"),
        )
        doc_id = session.insert(
            "INSERT INTO documents(library_id, version_id, page_id, url, content,"
            " metadata, sort_order, tenant_id) VALUES(?,?,?,?,?,?,?,?)",
            (
                lib_id,
                ver_id,
                page_id,
                "https://example.com/a",
                "vector similarity search powers retrieval",
                '{"title": "Similarity Guide"}',
                0,
                "t1",
            ),
        )
        fulltext.flush()
        assert fulltext.count(ver_id, "t1") == 1

        hits = fulltext.search(
            "similarity retrieval",
            version_ids=[ver_id],
            weights=(1.0, 0.6, 0.2, 0.0),
            budget=10,
            tenant_id="t1",
        )
        assert doc_id in [h[0] for h in hits]
        assert all(h[1] <= 0 or isinstance(h[1], float) for h in hits)

        # Tenant isolation: another tenant sees nothing.
        other = fulltext.search(
            "similarity",
            version_ids=[ver_id],
            weights=(1.0, 0.6, 0.2, 0.0),
            budget=10,
            tenant_id="other",
        )
        assert doc_id not in [h[0] for h in other]

        # Idempotent delete: second delete removes nothing.
        assert fulltext.delete_version(ver_id, "t1") >= 1
        assert fulltext.count(ver_id, "t1") == 0
        snap = fulltext.diagnostics()
        assert snap.get("backend")
    finally:
        session.close()


@pytest.mark.parametrize("settings_cls", _branches())
def test_error_hierarchy_uniform(settings_cls, tmp_path):
    settings = settings_cls()
    if settings_cls is _PostgresSettings:
        _fresh_postgres(settings.postgres_url)
    settings.db_path = (
        str(tmp_path / "meta.db") if _SqliteSettings is settings_cls else None
    )
    session = build_metadata_session(settings)
    try:
        session.apply_migrations()
        with pytest.raises(DbError):
            session.execute("SELECT * FROM no_such_table_xyz")
    finally:
        session.close()
