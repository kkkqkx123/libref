"""Backend-neutral storage contracts.

The metadata session is the lowest storage seam: it carries SQL text plus
parameters and returns plain rows, while each backend owns connection
lifecycle, transactions, schema migrations and error classification. The
fulltext contract is separate because keyword retrieval has no portable SQL
shape. Callers above this module never touch driver types.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable


class DbError(Exception):
    """Base class for storage failures that cross a backend boundary."""


TENANT_EXEMPT_TABLES = frozenset(
    {"robots_cache", "registry_cache", "discovery_cache", "schema_meta"}
)
"""Tables that never carry a tenant column and are always instance-local."""


class DbIntegrityError(DbError):
    """Unique or foreign-key constraint violation; safe to treat as a conflict."""


class DbTimeoutError(DbError):
    """Transient contention or connection timeout; safe to retry."""


class DbOperationalError(DbError):
    """Backend could not complete the operation."""


@runtime_checkable
class Cursor(Protocol):
    """Minimal read surface shared by every backend."""

    def fetchone(self) -> tuple[Any, ...] | None: ...

    def fetchall(self) -> list[tuple[Any, ...]]: ...

    @property
    def rowcount(self) -> int: ...

    @property
    def lastrowid(self) -> int | None: ...


@runtime_checkable
class Dialect(Protocol):
    """SQL fragments that differ between embedded and shared backends."""

    def now(self) -> str:
        """UTC timestamp literal in the backend's text format."""
        ...

    def json_extract(self, column: str, path: str) -> str:
        """Scalar JSON field extraction returning text."""
        ...

    def epoch_seconds(self, column: str) -> str:
        """Unix epoch expression for a timestamp column."""
        ...

    def insert_or_ignore(self, sql: str) -> str:
        """Rewrite an `INSERT OR IGNORE` statement for the backend."""
        ...


@runtime_checkable
class MetadataSession(Protocol):
    """One logical storage session; the only handle services receive."""

    dialect: Dialect

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Cursor: ...

    def executemany(self, sql: str, seq: Iterable[tuple[Any, ...]]) -> Cursor: ...

    def insert(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """Run one insert and return the new row id."""
        ...

    def executescript(self, script: str) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...

    def write_lock(self) -> AbstractContextManager[Any]:
        """Guard synchronous statement batches on this session."""
        ...

    def table_columns(self, table: str) -> set[str]:
        """Column names of one table; schema knowledge stays in the backend."""
        ...

    def table_has_tenant(self, table: str) -> bool:
        """Whether the table carries a tenant column; exemption lists live here."""
        ...

    def apply_migrations(self) -> None:
        """Create or advance the schema; idempotent."""
        ...

    def diagnostics(self) -> dict[str, Any]:
        """Backend-neutral health snapshot."""
        ...


@runtime_checkable
class FulltextStore(Protocol):
    """Keyword retrieval over document chunks, independent of the SQL dialect."""

    def search(
        self,
        query: str,
        *,
        version_ids: list[int],
        weights: tuple[float, float, float, float],
        budget: int,
        tenant_id: str | None = None,
    ) -> list[tuple[int, float]]: ...

    def delete_version(self, version_id: int, tenant_id: str | None = None) -> int: ...

    def count(self, version_id: int, tenant_id: str | None = None) -> int: ...

    def flush(self) -> None:
        """Make batch writes visible to search; no-op when writes are immediate."""
        ...

    def diagnostics(self) -> dict[str, Any]: ...
