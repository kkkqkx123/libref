"""Migration tests: replayable, version-recorded, all tables present (one of the P0 minimal checks)."""

from __future__ import annotations

import sqlite3

import pytest

from libref.db import MIGRATIONS, applied_versions, apply_migrations, open_db

pytestmark = pytest.mark.integration

CORE_TABLES = {"libraries", "versions", "pages", "documents", "documents_fts"}
EXT_TABLES = {"api_entities", "call_instances", "seq_patterns", "review_status"}


def _user_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def test_replay_is_noop(conn: sqlite3.Connection) -> None:
    assert apply_migrations(conn) == []
    assert applied_versions(conn) == {m.version for m in MIGRATIONS}


def test_fresh_db_applies_all_migrations(tmp_path) -> None:
    c = open_db(tmp_path / "fresh.db")
    assert apply_migrations(c) == [m.version for m in MIGRATIONS]
    c.close()


def test_all_tables_created(conn: sqlite3.Connection) -> None:
    tables = _user_tables(conn)
    assert CORE_TABLES | EXT_TABLES | {"schema_migrations"} <= tables


def test_reopen_replay_noop(tmp_path) -> None:
    path = tmp_path / "reopen.db"
    c1 = open_db(path)
    apply_migrations(c1)
    c1.close()
    c2 = open_db(path)
    assert apply_migrations(c2) == []
    assert CORE_TABLES | EXT_TABLES <= _user_tables(c2)
    c2.close()


def test_failed_migration_rolls_back(tmp_path, monkeypatch) -> None:
    c = open_db(tmp_path / "fail.db")
    # inject a bad migration: a failed run must not record a version number
    from libref.db import migrations as m

    bad_version = max(x.version for x in m.MIGRATIONS) + 1
    bad = m.Migration(bad_version, "bad", "CREATE TABLE broken(;")
    monkeypatch.setattr(m, "MIGRATIONS", (*m.MIGRATIONS, bad))
    with pytest.raises(sqlite3.OperationalError):
        m.apply_migrations(c)
    assert applied_versions(c) == {x.version for x in m.MIGRATIONS} - {bad_version}
    assert "broken" not in _user_tables(c)
    c.close()
