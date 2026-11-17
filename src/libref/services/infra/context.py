"""Shared runtime context for combined Web and MCP serving.

The context owns the storage bundle assembled from settings; request-scoped
services receive the metadata session instead of raw driver connections, so
backend selection stays in the assembly layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from libref.db.contract import MetadataSession
from libref.db.storage_bundle import Bundle


@dataclass
class AppContext:
    """Generation-guarded runtime: settings snapshot plus shared dependencies."""

    settings: Any
    db_path: Any  # retained for instance-local caches (simvec roots, snapshots)
    gateway: Any = None
    vec_store: Any = None
    embed: Any = None
    generation: int = 0
    job_manager: Any = None
    storage: Bundle | None = None

    def connect(self, *, check_same_thread: bool = True) -> MetadataSession:
        """Open the metadata session for one request or worker scope.

        The SQLite branch opens a short-lived session and runs migrations once
        per call cheaply (idempotent); the PostgreSQL branch returns the pooled
        session whose named operations are atomic on their own.
        """
        from libref.db.assembly import build_metadata_session

        backend = getattr(self.settings, "metadata_backend", "sqlite")
        if backend != "sqlite":
            if self.storage is not None:
                return self.storage.metadata
            session = build_metadata_session(self.settings)
            session.apply_migrations()
            return session
        from pathlib import Path

        from libref.db.sqlite_session import SqliteSession

        path = Path(str(self.db_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        # Fresh per-request session: sharing one connection across server threads
        # trips the driver thread-affinity check.
        session = SqliteSession.open(path, check_same_thread=check_same_thread)
        session.apply_migrations()
        return session

    def services_for(self, conn: MetadataSession):
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


def _instance_db_path(settings) -> Any:
    from pathlib import Path

    return Path(str(settings.db_path))


def build_context(settings) -> AppContext:
    """Assemble storage bundle, gateway, vectors and job state for one snapshot."""
    from libref.db.assembly import build_storage_bundle
    from libref.services.factory import build_gateway
    from libref.services.infra.vectors import build_vector

    gateway = build_gateway(settings)
    vec_store, embed, gateway = build_vector(settings, gateway)
    try:
        storage = build_storage_bundle(settings, vector=vec_store)
    except Exception:  # noqa: BLE001 — remote backends may be unwired in this build
        storage = None
    ctx = AppContext(
        settings=settings,
        db_path=_instance_db_path(settings),
        gateway=gateway,
        vec_store=vec_store,
        embed=embed,
        generation=0,
        job_manager=None,
        storage=storage,
    )
    return ctx


def refresh_context(ctx: AppContext, settings) -> AppContext:
    """Rebuild derived dependencies for a new frozen snapshot."""
    from libref.db.assembly import build_storage_bundle
    from libref.observe import setup_logging
    from libref.services.factory import build_gateway
    from libref.services.infra.vectors import build_vector

    gateway = build_gateway(settings)
    vec_store, embed, gateway = build_vector(settings, gateway)
    setup_logging(settings)
    try:
        storage = build_storage_bundle(settings, vector=vec_store)
    except Exception:  # noqa: BLE001 — remote backends may be unwired in this build
        storage = None
    return AppContext(
        settings=settings,
        db_path=_instance_db_path(settings),
        gateway=gateway,
        vec_store=vec_store,
        embed=embed,
        generation=ctx.generation + 1,
        job_manager=ctx.job_manager,
        storage=storage,
    )
