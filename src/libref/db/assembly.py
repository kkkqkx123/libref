"""Storage assembly: build a bundle from settings.

Backend selection happens here and only here. Remote branches are rejected at
assembly time until their implementation lands, matching the project's
forward-scaffolding convention.
"""

from __future__ import annotations

from libref.db.contract import FulltextStore, MetadataSession
from libref.db.sqlite_session import SqliteSession
from libref.db.storage_bundle import Bundle


def build_metadata_session(settings) -> MetadataSession:
    """Open the metadata backend selected by settings."""
    backend = getattr(settings, "metadata_backend", "sqlite")
    if backend == "sqlite":
        return SqliteSession.open(settings.db_path)
    if backend == "postgres":
        from libref.db.postgres_session import PostgresSession

        if not getattr(settings, "postgres_url", None):
            raise ValueError("postgres_url is required for metadata_backend='postgres'")
        return PostgresSession(
            settings.postgres_url,
            pool_size=getattr(settings, "postgres_pool_size", 8),
            timeout=getattr(settings, "postgres_timeout", 10.0),
        )
    raise ValueError(
        f"metadata backend {backend!r} is not wired yet; "
        "only 'sqlite' is supported in this build"
    )


def build_fulltext_store(settings, metadata: MetadataSession) -> FulltextStore:
    """Build the full-text backend selected by settings."""
    backend = getattr(settings, "fulltext_backend", "fts5")
    if backend == "fts5":
        from libref.store.fts5_store import Fts5FulltextStore

        return Fts5FulltextStore(metadata)
    raise ValueError(
        f"full-text backend {backend!r} is not wired yet; "
        "only 'fts5' is supported in this build"
    )


def build_storage_bundle(settings, *, vector=None) -> Bundle:
    """Assemble all three backends into one bundle."""
    metadata = build_metadata_session(settings)
    fulltext = build_fulltext_store(settings, metadata)
    if vector is None:
        from libref.llm import LlmGateway
        from libref.services.infra.vectors import build_vector

        gateway = LlmGateway(settings)
        vector, _embed, _gw = build_vector(settings, gateway)
    return Bundle(metadata=metadata, fulltext=fulltext, vector=vector)
