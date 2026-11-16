"""Storage contract smoke tests: SqliteSession delegates and dialect helpers."""

from __future__ import annotations

import tempfile
from pathlib import Path

from libref.db import apply_migrations, open_db
from libref.db.contract import DbIntegrityError
from libref.db.sqlite_session import SqliteSession


def test_sqlite_session_delegates() -> None:
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(Path(d) / "t.db")
        apply_migrations(conn)
        session = SqliteSession(conn)
        session.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
        session.commit()
        sid = session.insert("INSERT INTO t(v) VALUES(?)", ("hello",))
        assert sid >= 1
        row = session.execute("SELECT v FROM t WHERE id=?", (sid,)).fetchone()
        assert row is not None and row[0] == "hello"
        assert session.table_columns("t") == {"id", "v"}
        assert session.table_has_tenant("t") is False
        assert session.diagnostics()["backend"] == "sqlite"
        session.close()


def test_integrity_error_wrapped() -> None:
    with tempfile.TemporaryDirectory() as d:
        conn = open_db(Path(d) / "t.db")
        apply_migrations(conn)
        session = SqliteSession(conn)
        session.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT UNIQUE)")
        session.commit()
        session.insert("INSERT INTO t(v) VALUES(?)", ("x",))
        session.commit()
        try:
            session.insert("INSERT INTO t(v) VALUES(?)", ("x",))
        except DbIntegrityError:
            pass
        else:
            raise AssertionError("expected DbIntegrityError")
        session.close()


def test_dialect_fragments() -> None:
    from libref.db.sqlite_session import SqliteDialect

    d = SqliteDialect()
    assert "json_extract" in d.json_extract("m", "$.title")
    assert "datetime" in d.now()
