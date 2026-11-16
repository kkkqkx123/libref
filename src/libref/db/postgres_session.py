"""PostgreSQL metadata session: shared backend, forward scaffolding.

The connection and pool wiring is present so that configuration and assembly
can resolve the backend, but query execution is not implemented. Any call
raises a clear error so the assembly layer remains the single rejection point.
"""

from __future__ import annotations

from typing import Any


class PostgresDialect:
    def now(self) -> str:
        return "now()"

    def json_extract(self, column: str, path: str) -> str:
        return f"{column}->>'{path.lstrip('$.')}'"

    def epoch_seconds(self, column: str) -> str:
        return f"EXTRACT(EPOCH FROM {column})"

    def insert_or_ignore(self, sql: str) -> str:
        return sql.replace("INSERT OR IGNORE", "INSERT ... ON CONFLICT DO NOTHING")


class PostgresSession:
    """Placeholder session; raises on use until the backend is implemented."""

    dialect = PostgresDialect()

    def __init__(self, url: str, *, pool_size: int = 8, timeout: float = 10.0) -> None:
        self._url = url
        self._pool_size = pool_size
        self._timeout = timeout
        self._conn = None

    def _not_wired(self) -> None:
        raise NotImplementedError(
            "PostgreSQL metadata backend is not wired in this build; "
            "use metadata_backend='sqlite'"
        )

    def execute(self, sql: str, params: tuple[Any, ...] = ()):
        self._not_wired()

    def executemany(self, sql: str, seq=()):
        self._not_wired()

    def insert(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        self._not_wired()

    def executescript(self, script: str) -> None:
        self._not_wired()

    def commit(self) -> None:
        self._not_wired()

    def rollback(self) -> None:
        self._not_wired()

    def close(self) -> None:
        return None

    def write_lock(self):
        import contextlib

        return contextlib.nullcontext()

    def table_columns(self, table: str) -> set[str]:
        self._not_wired()

    def table_has_tenant(self, table: str) -> bool:
        return False

    def apply_migrations(self) -> None:
        self._not_wired()

    def diagnostics(self) -> dict[str, Any]:
        return {"backend": "postgres", "wired": False}
