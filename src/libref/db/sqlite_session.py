"""SQLite metadata session: embedded backend over the standard library driver."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from libref.db.contract import (
    TENANT_EXEMPT_TABLES,
    Cursor,
    DbError,
    DbIntegrityError,
    DbOperationalError,
    DbTimeoutError,
    Dialect,
)


class SqliteDialect:
    """SQLite SQL fragments."""

    def now(self) -> str:
        return "datetime('now')"

    def json_extract(self, column: str, path: str) -> str:
        return f"json_extract({column}, '{path}')"

    def epoch_seconds(self, column: str) -> str:
        return f"CAST(strftime('%s', {column}) AS REAL)"

    def insert_or_ignore(self, sql: str) -> str:
        return sql


class SqliteSession:
    """One SQLite connection plus backend-neutral error classification."""

    dialect: Dialect = SqliteDialect()

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._guard = threading.RLock()

    @classmethod
    def open(cls, path: str | Path, *, check_same_thread: bool = True) -> SqliteSession:
        """Open a database file and apply the PRAGMA set used everywhere."""
        from libref.db.conn import open_db

        return cls(open_db(path, check_same_thread=check_same_thread))

    @property
    def raw(self) -> sqlite3.Connection:
        """Underlying driver connection; reserved for backend diagnostics."""
        return self._conn

    def _wrap(self, exc: sqlite3.Error) -> DbError:
        if isinstance(exc, sqlite3.IntegrityError):
            return DbIntegrityError(str(exc))
        if isinstance(exc, sqlite3.OperationalError):
            text = str(exc).lower()
            if "locked" in text or "busy" in text or "timeout" in text:
                return DbTimeoutError(str(exc))
            return DbOperationalError(str(exc))
        return DbOperationalError(str(exc))

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Cursor:
        try:
            return self._conn.execute(sql, params)
        except sqlite3.Error as exc:
            raise self._wrap(exc) from exc

    def executemany(self, sql: str, seq: Iterable[tuple[Any, ...]]) -> Cursor:
        try:
            return self._conn.executemany(sql, list(seq))
        except sqlite3.Error as exc:
            raise self._wrap(exc) from exc

    def insert(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        cur = self.execute(sql, params)
        return int(cur.lastrowid or 0)

    def executescript(self, script: str) -> None:
        try:
            self._conn.executescript(script)
        except sqlite3.Error as exc:
            raise self._wrap(exc) from exc

    def commit(self) -> None:
        try:
            self._conn.commit()
        except sqlite3.Error as exc:
            raise self._wrap(exc) from exc

    def rollback(self) -> None:
        try:
            self._conn.rollback()
        except sqlite3.Error as exc:
            raise self._wrap(exc) from exc

    def close(self) -> None:
        self._conn.close()

    def write_lock(self):
        return self._guard

    def table_columns(self, table: str) -> set[str]:
        # table_xinfo includes generated columns (title/path), which table_info
        # hides on some builds; every consumer only tests membership.
        rows = self._conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
        return {str(row[1]) for row in rows}

    def table_has_tenant(self, table: str) -> bool:
        if table in TENANT_EXEMPT_TABLES:
            return False
        return "tenant_id" in self.table_columns(table)

    def apply_migrations(self) -> None:
        from libref.db.migrations import apply_migrations

        apply_migrations(self._conn)

    def diagnostics(self) -> dict[str, Any]:
        try:
            page_count = int(self._conn.execute("PRAGMA page_count").fetchone()[0])
            freelist = int(self._conn.execute("PRAGMA freelist_count").fetchone()[0])
        except sqlite3.Error:
            page_count = 0
            freelist = 0
        return {
            "backend": "sqlite",
            "path": str(getattr(self._conn, "database", "")),
            "page_count": page_count,
            "freelist_count": freelist,
        }
