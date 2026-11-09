"""Composition root: single place that wires gateway, vectors, and services.

Exits (cli, mcp_server, web) only call this factory instead of
constructing QdrantVecStore and LlmGateway inline.
"""

from __future__ import annotations


def build_gateway(settings):
    """Build the shared LLM gateway for the given settings."""
    from libref.llm import LlmGateway

    return LlmGateway(settings)


def build_vector(settings, gateway=None):
    """Build vector store and embed function, returning (None, None) when disabled."""
    from libref.llm import LlmGateway
    from libref.store.vec_store import QdrantVecStore, collection_for

    gw = gateway if gateway is not None else LlmGateway(settings)
    embed = gw.embed_fn()
    if embed is None:
        return None, None, gw
    vec = QdrantVecStore(
        settings.qdrant_url,
        collection_for(settings.qdrant_collection, settings.db_path),
        timeout=settings.qdrant_timeout,
    )
    return vec, embed, gw


def build_search_service(
    conn, settings, *, gateway=None, vec_store=None, embed=None, rerank=None
):
    """Wire a SearchService with vectors when keys are present, else pure FTS."""
    from libref.services.search import SearchService

    gw = gateway if gateway is not None else build_gateway(settings)
    vec = vec_store
    fn = embed
    if vec is None or fn is None:
        built_vec, built_embed, gw = build_vector(settings, gw)
        if vec is None:
            vec = built_vec
        if fn is None:
            fn = built_embed
    rerank_fn = rerank
    if rerank_fn is None:
        try:
            rerank_fn = gw.rerank_fn()
        except Exception:  # noqa: BLE001 — rerank is optional; search runs without it
            rerank_fn = None
    service = SearchService(conn, settings, vec_store=vec, embed=fn, rerank=rerank_fn)
    return service, gw


def build_ingest_service(conn, settings, *, gateway=None, vec_store=None, embed=None):
    """Assemble the doc scrape service; degrades to pure source-of-truth storage when vectors are missing."""
    from libref.services.ingest import IngestService

    gw = gateway if gateway is not None else build_gateway(settings)
    vec = vec_store
    fn = embed
    if vec is None or fn is None:
        built_vec, built_embed, gw = build_vector(settings, gw)
        if vec is None:
            vec = built_vec
        if fn is None:
            fn = built_embed
    return IngestService(settings, conn, vec_store=vec, embed=fn), gw


def build_maintenance_service(
    conn, settings, *, gateway=None, vec_store=None, embed=None
):
    """Assemble the vector re-ingest service; callers no longer hand-wire the gateway and vectors."""
    from libref.services.maintenance import MaintenanceService

    gw = gateway if gateway is not None else build_gateway(settings)
    vec = vec_store
    fn = embed
    if vec is None or fn is None:
        built_vec, built_embed, gw = build_vector(settings, gw)
        if vec is None:
            vec = built_vec
        if fn is None:
            fn = built_embed
    return MaintenanceService(conn, settings, vec_store=vec, embed=fn), gw
