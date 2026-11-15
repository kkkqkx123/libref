"""Shared runtime context for combined Web and MCP serving."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
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
        from libref.db import open_db

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        return open_db(self.db_path, check_same_thread=check_same_thread)


def build_context(settings) -> AppContext:
    """Assemble gateway, vectors, and job state for one settings snapshot."""
    from libref.services.factory import build_gateway
    from libref.services.infra.jobs import JobManager
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
    from libref.services.factory import build_gateway
    from libref.services.infra.vectors import build_vector
    from libref.observe import setup_logging

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
