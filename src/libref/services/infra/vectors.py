"""Shared gateway and vector wiring; no domain imports, only llm and store leaves."""

from __future__ import annotations


def build_gateway(settings):
    """Build the shared LLM gateway for the given settings."""
    from libref.llm import LlmGateway

    return LlmGateway(settings)


def build_vector(settings, gateway=None):
    """Build vector store and embed function, returning (None, None) when disabled."""
    from libref.llm import LlmGateway
    from libref.store.vec_store import (
        QdrantVecStore,
        SimvecVecStore,
        collection_for,
        simvec_root_for,
    )

    gw = gateway if gateway is not None else LlmGateway(settings)
    embed = gw.embed_fn()
    if embed is None:
        return None, None, gw
    if settings.vector_backend == "simvec":
        vec = SimvecVecStore(
            simvec_root_for(settings.db_path, settings.simvec_dir),
            collection_for(settings.vector_collection, settings.db_path),
            lib=settings.simvec_lib,
        )
    else:
        vec = QdrantVecStore(
            settings.qdrant_url,
            collection_for(settings.vector_collection, settings.db_path),
            timeout=settings.qdrant_timeout,
        )
    return vec, embed, gw
