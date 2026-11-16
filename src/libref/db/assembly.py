"""Storage assembly: build a bundle from settings.

Backend selection for the metadata session and the full-text store happens here
and only here. The vector handle is built by the settings-driven factory in the
services layer and injected, because its construction needs the LLM gateway and
must not be reached from the database layer.
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
    if backend == "postgres":
        from libref.store.postgres_fts import PostgresFulltextStore

        return PostgresFulltextStore(metadata)
    raise ValueError(
        f"full-text backend {backend!r} is not wired yet; "
        "only 'fts5' is supported in this build"
    )


def build_storage_bundle(settings, *, vector) -> Bundle:
    """Assemble all three backends into one bundle.

    The vector handle is built by the layer that owns settings-driven backend
    selection and injected here; this module must not reach upward into services.
    """
    metadata = build_metadata_session(settings)
    fulltext = build_fulltext_store(settings, metadata)
    return Bundle(metadata=metadata, fulltext=fulltext, vector=vector)
