"""PostgreSQL metadata session: shared backend over a psycopg3 connection pool.

Every named operation borrows a pooled connection and is atomic on its own,
matching the contract rule that each operation commits independently. Multi-
statement batches use `session()` to hold one connection across an explicit
commit/rollback. Services never see driver types: errors are classified into
the contract hierarchy and cursors are adapted to the neutral surface.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable
from typing import Any, Self

from libref.db.contract import (
    TENANT_EXEMPT_TABLES,
    Cursor,
    DbError,
    DbIntegrityError,
    DbOperationalError,
    DbTimeoutError,
    Dialect,
)


class PostgresDialect:
    """PostgreSQL SQL fragments."""

    def now(self) -> str:
        return "to_char(now(), 'YYYY-MM-DD HH24:MI:SS')"

    def json_extract(self, column: str, path: str) -> str:
        steps = [step for step in path.lstrip("$. ").split(".") if step]
        expr = column
        for step in steps[:-1]:
            expr = f"{expr}->'{step}'"
        if steps:
            expr = f"{expr}->>'{steps[-1]}'"
        return expr

    def epoch_seconds(self, column: str) -> str:
        return f"EXTRACT(EPOCH FROM {column}::timestamptz)"

    def insert_or_ignore(self, sql: str) -> str:
        return sql.replace("INSERT OR IGNORE", "INSERT")


_RETURNING_RE = re.compile(r"\bRETURNING\b", re.IGNORECASE)
_STRING_SPLIT_RE = re.compile(r"('(?:[^']|'')*')", re.DOTALL)


def _to_pg_sql(sql: str) -> str:
    """Rewrite `?` placeholders and `INSERT OR IGNORE` for PostgreSQL.

    Placeholders inside single-quoted string literals are preserved. Only
    statements that arrive with `INSERT OR IGNORE` gain the conflict clause —
    plain inserts must keep raising integrity errors. The clause is inserted
    before an explicit RETURNING when present.
    """
    parts = _STRING_SPLIT_RE.split(sql)
    for i, part in enumerate(parts):
        if i % 2 == 0:
            parts[i] = part.replace("?", "%s")
    out = "".join(parts)
    is_insert_or_ignore = bool(
        re.search(r"\bINSERT\s+OR\s+IGNORE\b", out, re.IGNORECASE)
    )
    out = PostgresDialect().insert_or_ignore(out)
    if is_insert_or_ignore and "ON CONFLICT" not in out:
        if _RETURNING_RE.search(out):
            out = _RETURNING_RE.sub("ON CONFLICT DO NOTHING RETURNING", out, count=1)
        else:
            out += " ON CONFLICT DO NOTHING"
    return out


def _split_script(script: str) -> list[str]:
    """Split a DDL script into statements; dollar-quoted bodies are not used."""
    return [stmt.strip() + ";" for stmt in script.split(";") if stmt.strip()]


class _CursorAdapter:
    """Cursor surface over rows materialized before the pooled connection returns.

    One-shot statements borrow a pooled connection only for the statement, so
    result rows must be fetched eagerly: once the connection goes back to the
    pool its cursor is no longer valid. Row count and rows are captured at
    execution time; fetchone/fetchall consume the materialized rows.
    """

    def __init__(self, cur: Any, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self._pos = 0
        self._rowcount = int(cur.rowcount or 0)

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list[tuple[Any, ...]]:
        rest = self._rows[self._pos :]
        self._pos = len(self._rows)
        return rest

    @property
    def rowcount(self) -> int:
        return self._rowcount

    @property
    def lastrowid(self) -> int | None:
        return None


class _StatementMixin:
    """Shared statement surface over one live driver connection."""

    dialect: Dialect = PostgresDialect()

    _conn: Any
    _wrap: Any  # provided by the owner

    def _cursor(self) -> Any:
        assert self._conn is not None, "no active connection"
        return self._conn.cursor()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Cursor:
        try:
            cur = self._cursor()
            cur.execute(_to_pg_sql(sql), tuple(params))
            # DML without RETURNING produces no records; fetchall would raise.
            rows = [tuple(r) for r in cur.fetchall()] if cur.description else []
            return _CursorAdapter(cur, rows)
        except Exception as exc:
            raise self._wrap(exc) from exc

    def executemany(self, sql: str, seq: Iterable[tuple[Any, ...]]) -> Cursor:
        try:
            cur = self._cursor()
            cur.executemany(_to_pg_sql(sql), [tuple(p) for p in seq])
            rows: list[tuple[Any, ...]] = []
            return _CursorAdapter(cur, rows)
        except Exception as exc:
            raise self._wrap(exc) from exc

    def insert(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        rewritten = _to_pg_sql(sql)
        if not _RETURNING_RE.search(rewritten):
            rewritten += " RETURNING id"
        try:
            cur = self._cursor()
            cur.execute(rewritten, tuple(params))
            row = cur.fetchone()
            return int(row[0]) if row else 0
        except Exception as exc:
            raise self._wrap(exc) from exc

    def executescript(self, script: str) -> None:
        for statement in _split_script(script):
            if statement.strip():
                self.execute(statement)


class PostgresSession(_StatementMixin):
    """Pooled PostgreSQL session implementing the metadata contract."""

    dialect: Dialect = PostgresDialect()

    def __init__(self, url: str, *, pool_size: int = 8, timeout: float = 10.0) -> None:
        from psycopg_pool import ConnectionPool  # type: ignore[import-not-found]

        self._guard = threading.RLock()
        self._max_size = pool_size
        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=pool_size,
            timeout=timeout,
            open=True,
            kwargs={"autocommit": True},
        )

    @classmethod
    def open(
        cls, url: str, *, pool_size: int = 8, timeout: float = 10.0
    ) -> PostgresSession:
        return cls(url, pool_size=pool_size, timeout=timeout)

    def _borrow(self):
        return self._pool.connection()

    def _wrap(self, exc: Exception) -> DbError:
        cls = type(exc).__name__
        text = str(exc).lower()
        if cls in (
            "UniqueViolation",
            "ForeignKeyViolation",
            "CheckViolation",
            "NotNullViolation",
        ):
            return DbIntegrityError(str(exc))
        if (
            "timeout" in text
            or "could not connect" in text
            or cls in ("OperationalError", "ConnectionPoolTimeout", "PoolTimeout")
        ):
            return DbTimeoutError(str(exc))
        return DbOperationalError(str(exc))

    # One-shot statement surface: each call is its own atomic unit.
    def _oneshot(self):
        return _OneShot(self)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Cursor:
        with self._oneshot() as scope:
            return scope.execute(sql, params)

    def executemany(self, sql: str, seq: Iterable[tuple[Any, ...]]) -> Cursor:
        with self._oneshot() as scope:
            return scope.executemany(sql, seq)

    def insert(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._oneshot() as scope:
            return scope.insert(sql, params)

    def executescript(self, script: str) -> None:
        with self._oneshot() as scope:
            scope.executescript(script)

    def session(self) -> _SessionScope:
        """Hold one pooled connection across an explicit transaction batch."""
        return _SessionScope(self)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self._pool.close()

    def write_lock(self):
        return self._guard

    def table_columns(self, table: str) -> set[str]:
        with self._borrow() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = %s",
                (table,),
            )
            return {str(row[0]) for row in cur.fetchall()}

    def table_has_tenant(self, table: str) -> bool:
        if table in TENANT_EXEMPT_TABLES:
            return False
        return "tenant_id" in self.table_columns(table)

    def apply_migrations(self) -> None:
        from libref.db.migrations import MIGRATIONS
        from libref.db.postgres_schema import POSTGRES_SCHEMA_SQL

        with self._borrow() as conn, conn.cursor() as cur:
            cur.execute(POSTGRES_SCHEMA_SQL)
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations("
                " version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                " applied_at TEXT NOT NULL DEFAULT"
                " to_char(now(), 'YYYY-MM-DD HH24:MI:SS'))"
            )
            cur.execute("SELECT count(*) FROM schema_migrations")
            done = int(cur.fetchone()[0])
            if done >= len(MIGRATIONS):
                return
            for version in range(done, len(MIGRATIONS)):
                cur.execute(
                    "INSERT INTO schema_migrations(version, name) VALUES(%s, %s)"
                    " ON CONFLICT (version) DO NOTHING",
                    (version, f"consolidated-{version}"),
                )

    def diagnostics(self) -> dict[str, Any]:
        info: dict[str, Any] = {"backend": "postgres", "pool_size": self._max_size}
        try:
            with self._borrow() as conn, conn.cursor() as cur:
                cur.execute("SHOW server_version")
                info["server_version"] = str(cur.fetchone()[0])
                info["reachable"] = True
        except Exception as exc:  # noqa: BLE001 — diagnostics must not raise
            info["reachable"] = False
            info["error"] = str(exc)
        return info


class _OneShot(_StatementMixin):
    """Borrows a connection for exactly one statement; no transaction control."""

    def __init__(self, parent: PostgresSession) -> None:
        self._parent = parent
        self._cm: Any = None
        self._conn = None

    def __enter__(self) -> Self:
        # Keep the pool's context manager: psycopg3's Connection.__exit__
        # closes the connection, which would poison the pool.
        self._cm = self._parent._borrow()
        self._conn = self._cm.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._cm is not None:
            self._cm.__exit__(exc_type, exc, tb)
            self._cm = None
            self._conn = None

    def _wrap(self, exc: Exception) -> DbError:
        return self._parent._wrap(exc)


class _SessionScope(_StatementMixin):
    """One checked-out pooled connection across a transaction batch."""

    def __init__(self, parent: PostgresSession) -> None:
        self._parent = parent
        self._cm: Any = None
        self._conn = None
        self._entered_at = time.monotonic()

    def __enter__(self) -> Self:
        # Same pool-context-manager rule as _OneShot: never close a pooled
        # connection directly.
        self._cm = self._parent._borrow()
        self._conn = self._cm.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._cm is not None:
            self._cm.__exit__(exc_type, exc, tb)
            self._cm = None
            self._conn = None

    def _wrap(self, exc: Exception) -> DbError:
        return self._parent._wrap(exc)

    def commit(self) -> None:
        if self._conn is not None:
            self._conn.commit()

    def rollback(self) -> None:
        if self._conn is not None:
            self._conn.rollback()

    def close(self) -> None:
        return None

    def write_lock(self):
        return self._parent.write_lock()

    def table_columns(self, table: str) -> set[str]:
        return self._parent.table_columns(table)

    def table_has_tenant(self, table: str) -> bool:
        return self._parent.table_has_tenant(table)

    def apply_migrations(self) -> None:
        self._parent.apply_migrations()

    def diagnostics(self) -> dict[str, Any]:
        return self._parent.diagnostics()
