"""Shared runtime context for combined Web and MCP serving."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class AppContext:
    """Generation-guarded runtime: settings snapshot plus shared dependencies."""

    settings: Any
    db_path: Path
    gateway: Any = None
    vec_store: Any = None
    embed: Any = None
    generation: int = 0
    job_manager: Any = None

    def connect(self, *, check_same_thread: bool = True) -> sqlite3.Connection:
        """Open a short-lived connection; migrations run once at startup."""
        from libref.db import apply_migrations, open_db

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = open_db(self.db_path, check_same_thread=check_same_thread)
        try:
            apply_migrations(conn)
        except Exception:  # noqa: BLE001, S110
            pass
        return conn

    def services_for(self, conn):
        """Build request-scoped services from the current generation dependencies."""
        from libref.services.code.mining_read import MiningReadService
        from libref.services.docs.pages import PageService
        from libref.services.docs.search import SearchService
        from libref.services.infra.jobs import JobManager
        from libref.services.ops.review import ReviewService
        from libref.services.registry.libraries import LibraryService

        gateway = self.gateway
        try:
            rerank = gateway.rerank_fn() if gateway is not None else None
        except Exception:  # noqa: BLE001
            rerank = None
        try:
            chat = gateway.chat_fn() if gateway is not None else None
        except Exception:  # noqa: BLE001
            chat = None
        search = SearchService(
            conn,
            self.settings,
            vec_store=self.vec_store,
            embed=self.embed,
            rerank=rerank,
        )
        return {
            "search": search,
            "pages": PageService(conn),
            "catalog": LibraryService(conn),
            "mining": MiningReadService(conn, self.settings),
            "review": ReviewService(conn),
            "jobs": JobManager(
                conn,
                instance_id=str(
                    getattr(self.settings, "instance_id", "libref-1") or "libref-1"
                ),
            ),
            "rerank": rerank,
            "chat": chat,
            "gateway": gateway,
            "vec_store": self.vec_store,
            "embed": self.embed,
            "generation": self.generation,
            "settings": self.settings,
        }


def build_context(settings) -> AppContext:
    """Assemble gateway, vectors, and job state for one settings snapshot."""
    from libref.services.factory import build_gateway
    from libref.services.infra.vectors import build_vector

    gateway = build_gateway(settings)
    vec_store, embed, gateway = build_vector(settings, gateway)
    ctx = AppContext(
        settings=settings,
        db_path=Path(str(settings.db_path)),
        gateway=gateway,
        vec_store=vec_store,
        embed=embed,
        generation=0,
        job_manager=None,
    )
    return ctx


def refresh_context(ctx: AppContext, settings) -> AppContext:
    """Rebuild derived dependencies for a new frozen snapshot."""
    from libref.observe import setup_logging
    from libref.services.factory import build_gateway
    from libref.services.infra.vectors import build_vector

    gateway = build_gateway(settings)
    vec_store, embed, gateway = build_vector(settings, gateway)
    setup_logging(settings)
    return AppContext(
        settings=settings,
        db_path=Path(str(settings.db_path)),
        gateway=gateway,
        vec_store=vec_store,
        embed=embed,
        generation=ctx.generation + 1,
        job_manager=ctx.job_manager,
    )
