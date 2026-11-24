"""Rebuildable discovery cache reads and writes."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any


class Cache:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def _read_cache(self, ecosystem: str, query: str) -> dict | None:
        row = self._conn.execute(
            "SELECT candidates, fetched_at FROM discovery_cache WHERE ecosystem=? AND query=?",
            (ecosystem, query.strip().lower()),
        ).fetchone()
        if row is None:
            return None
        try:
            candidates = json.loads(row[0]) if row[0] else []
        except ValueError:
            return None
        return {"candidates": candidates, "fetched_at": row[1]}

    def _write_cache(self, ecosystem: str, query: str, candidates: list[dict]) -> None:
        from libref.db.conn import write_lock

        with write_lock(self._conn):
            self._conn.execute(
                "INSERT INTO discovery_cache(ecosystem, query, candidates, fetched_at) VALUES(?,?,?,?) "
                "ON CONFLICT(ecosystem, query) DO UPDATE SET candidates=excluded.candidates, "
                "fetched_at=excluded.fetched_at",
                (
                    ecosystem,
                    query.strip().lower(),
                    json.dumps(candidates, ensure_ascii=False),
                    time.time(),
                ),
            )
            self._conn.commit()
