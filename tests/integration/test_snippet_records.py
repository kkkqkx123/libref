"""Snippet records upsert, tenant isolation, status listing and manual review."""

from __future__ import annotations

import os

import pytest

from libref.db.assembly import build_metadata_session

_PG_URL = os.environ.get("LIBREF_TEST_POSTGRES_URL", "")
needs_pg = pytest.mark.skipif(not _PG_URL, reason="LIBREF_TEST_POSTGRES_URL not set")


def _seed(conn, tenant: str) -> int:
    lib = conn.execute(
        "INSERT INTO libraries(ecosystem, name, tenant_id) VALUES(?, ?, ?)",
        ("pypi", f"snippets-{tenant}", tenant),
    )
    lib_id = int(lib.lastrowid)
    conn.execute(
        "INSERT INTO versions(library_id, version, tenant_id) VALUES(?, ?, ?)",
        (lib_id, "1", tenant),
    )
    return int(conn.execute("SELECT max(id) FROM versions").fetchone()[0])


def _record(version_id: int, snippet_id: str, status: str, tenant: str) -> dict:
    return {
        "version_id": version_id,
        "source_key": "doc:1:/api",
        "snippet_id": snippet_id,
        "code_hash": "hash-" + snippet_id,
        "code": "client.run()",
        "url": "https://example.test/api",
        "heading": "API",
        "ordinal": 1,
        "origin_kind": "markdown_fence",
        "origin_location": "https://example.test/api",
        "origin_start_byte": 0,
        "origin_end_byte": 12,
        "origin_start_line": 1,
        "origin_end_line": 1,
        "raw_language": "python",
        "language": "python",
        "language_source": "candidate",
        "language_rule": "",
        "parse_status": status,
        "attempts": "[]",
        "review_status": "pending",
        "tenant_id": tenant,
    }


def _assert_flow(session, tenant: str) -> None:
    version_id = _seed(session, tenant)
    from libref.services.code import snippet_records

    snippet_records.record_source_options(
        session, version_id, candidates=["python"], tenant_id=tenant
    )
    assert snippet_records.load_source_options(session, version_id, tenant) == {
        "candidates": ["python"],
        "rules": {},
    }
    snippet_records.upsert_snippet_record(
        session, _record(version_id, "s1", "ok", tenant)
    )
    snippet_records.upsert_snippet_record(
        session, _record(version_id, "s1", "partial", tenant)
    )
    assert (
        len(snippet_records.list_snippet_records(session, version_id, tenant_id=tenant))
        == 1
    )
    assert [
        row["parse_status"]
        for row in snippet_records.list_snippet_records(
            session, version_id, status="partial", tenant_id=tenant
        )
    ] == ["partial"]
    reviewed = snippet_records.annotate_snippet_record(
        session,
        version_id,
        "doc:1:/api",
        "s1",
        language="python",
        reviewer="reviewer",
        tenant_id=tenant,
    )
    assert reviewed is not None
    assert reviewed["review_status"] == "confirmed"
    assert reviewed["language_source"] == "manual_review"


def test_snippet_records_sqlite(tmp_path) -> None:
    class Settings:
        metadata_backend = "sqlite"
        fulltext_backend = "fts5"
        db_path = str(tmp_path / "snippets.db")

    session = build_metadata_session(Settings())
    session.apply_migrations()
    try:
        _assert_flow(session, "default")
    finally:
        session.close()


@needs_pg
def test_snippet_records_postgres() -> None:
    class Settings:
        metadata_backend = "postgres"
        fulltext_backend = "postgres"
        postgres_url = _PG_URL
        postgres_pool_size = 2
        postgres_timeout = 5.0

    from tests.integration.test_schema_parity import _fresh_postgres

    _fresh_postgres(_PG_URL)
    session = build_metadata_session(Settings())
    session.apply_migrations()
    try:
        _assert_flow(session, "default")
    finally:
        session.close()


def test_snippet_records_tenant_isolation(tmp_path) -> None:
    class Settings:
        metadata_backend = "sqlite"
        fulltext_backend = "fts5"
        db_path = str(tmp_path / "snippets.db")

    session = build_metadata_session(Settings())
    session.apply_migrations()
    first = _seed(session, "alpha")
    second = _seed(session, "beta")
    from libref.services.code import snippet_records

    snippet_records.upsert_snippet_record(session, _record(first, "s1", "ok", "alpha"))
    snippet_records.upsert_snippet_record(
        session, _record(second, "s1", "failed", "beta")
    )
    assert [
        row["parse_status"]
        for row in snippet_records.list_snippet_records(
            session, first, tenant_id="alpha"
        )
    ] == ["ok"]
    assert snippet_records.list_snippet_records(session, first, tenant_id="beta") == []
    session.close()
