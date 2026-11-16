"""FTS5 full-text backend over the SQLite metadata session.

The FTS5 virtual table and its triggers are maintained by the schema; this
layer only exposes the contract surface and delegates SQL to the session.
"""

from __future__ import annotations

from typing import Any


class Fts5FulltextStore:
    """Local full-text backend: FTS5 over the metadata session."""

    def __init__(self, session) -> None:
        self._session = session

    @property
    def raw(self):
        return getattr(self._session, "raw", self._session)

    def search(
        self,
        query: str,
        *,
        version_ids: list[int],
        weights: tuple[float, float, float, float],
        budget: int,
        tenant_id: str | None = None,
    ) -> list[tuple[int, float]]:
        from libref.store.retriever import fts_search

        return fts_search(
            self.raw,
            query,
            weights=weights,
            budget=budget,
            tenant_id=tenant_id,
        )

    def delete_version(self, version_id: int, tenant_id: str | None = None) -> int:
        return 0

    def count(self, version_id: int, tenant_id: str | None = None) -> int:
        row = self.raw.execute(
            "SELECT count(*) FROM documents WHERE version_id=?", (version_id,)
        ).fetchone()
        return int(row[0]) if row else 0

    def flush(self) -> None:
        return None

    def diagnostics(self) -> dict[str, Any]:
        return {"backend": "fts5"}
