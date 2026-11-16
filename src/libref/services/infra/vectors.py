"""Shared gateway and vector wiring; no domain imports, only llm and store leaves.

This module is the single place where the vector backend is chosen: it reads the
vector settings once, derives the collection profile and name, and hands the
resulting store to the assembly layer. Exits and services never branch on the
backend themselves.
"""

from __future__ import annotations


def profile_from_settings(settings):
    """Collection profile from settings, split into the shape and tuning segments.

    Shape fields can only be applied when the collection is created, so drift there
    triggers a rebuild; tuning fields are rewritten in place by the remote backend.
    Local engine graph parameters sit in the shape segment because the engine fixes
    them at creation time.
    """
    from libref.store.vec_store import VectorProfile

    return VectorProfile(
        backend=str(getattr(settings, "vector_backend", "simvec")),
        distance=str(getattr(settings, "vector_distance", "cosine")),
        index=str(getattr(settings, "simvec_index", "hnsw")),
        hnsw_m=int(getattr(settings, "simvec_hnsw_m", 16)),
        hnsw_ef_construct=int(getattr(settings, "simvec_hnsw_ef_construct", 100)),
        full_scan_threshold=int(getattr(settings, "simvec_full_scan_threshold", 10000)),
        ef_search=int(getattr(settings, "simvec_hnsw_ef_search", 40)),
        max_indexing_threads=getattr(settings, "simvec_max_indexing_threads", None),
        qdrant_on_disk_vectors=getattr(settings, "qdrant_on_disk_vectors", None),
        qdrant_on_disk_payload=getattr(settings, "qdrant_on_disk_payload", None),
        qdrant_shard_number=getattr(settings, "qdrant_shard_number", None),
        qdrant_replication_factor=getattr(settings, "qdrant_replication_factor", None),
        qdrant_write_consistency_factor=getattr(
            settings, "qdrant_write_consistency_factor", None
        ),
        qdrant_hnsw_m=getattr(settings, "qdrant_hnsw_m", None),
        qdrant_hnsw_ef_construct=getattr(settings, "qdrant_hnsw_ef_construct", None),
        qdrant_full_scan_threshold=getattr(
            settings, "qdrant_full_scan_threshold", None
        ),
        qdrant_memmap_threshold_kb=getattr(
            settings, "qdrant_memmap_threshold_kb", None
        ),
        qdrant_indexing_threshold_kb=getattr(
            settings, "qdrant_indexing_threshold_kb", None
        ),
    )


def collection_name_from_settings(settings) -> str:
    """Collection name from the declared deployment identity, else from the database file."""
    from libref.store.vec_store import collection_for_deployment

    deployment = str(
        getattr(settings, "deployment_id", "")
        or getattr(settings, "instance_id", "")
        or ""
    )
    return collection_for_deployment(
        str(settings.vector_collection), deployment, settings.db_path
    )


def build_gateway(settings):
    """Build the shared LLM gateway for the given settings."""
    from libref.llm import LlmGateway

    return LlmGateway(settings)


def build_store(settings):
    """Build only the vector store; health and maintenance commands need no embeddings."""
    from libref.store.vec_store import QdrantVecStore, SimvecVecStore, simvec_root_for

    collection = collection_name_from_settings(settings)
    profile = profile_from_settings(settings)
    write_batch = int(getattr(settings, "vector_write_batch", 512))
    max_points = getattr(settings, "vector_max_points", None)
    if settings.vector_backend == "simvec":
        return SimvecVecStore(
            simvec_root_for(settings.db_path, settings.simvec_dir),
            collection,
            lib=settings.simvec_lib,
            write_batch=write_batch,
            max_points=max_points,
            profile=profile,
        )
    return QdrantVecStore(
        settings.qdrant_url,
        collection,
        timeout=settings.qdrant_timeout,
        api_key=settings.qdrant_api_key,
        https=settings.qdrant_https,
        prefer_grpc=bool(getattr(settings, "qdrant_grpc", False)),
        grpc_port=int(getattr(settings, "qdrant_grpc_port", 6334)),
        prefix=getattr(settings, "qdrant_prefix", None),
        pool_size=getattr(settings, "qdrant_pool_size", None),
        search_hnsw_ef=settings.qdrant_search_hnsw_ef,
        search_exact=settings.qdrant_search_exact,
        write_batch=write_batch,
        max_points=max_points,
        profile=profile,
    )


def build_vector(settings, gateway=None):
    """Build vector store and embed function, returning (None, None, gateway) when disabled."""
    from libref.llm import LlmGateway

    gw = gateway if gateway is not None else LlmGateway(settings)
    embed = gw.embed_fn()
    if embed is None:
        return None, None, gw
    return build_store(settings), embed, gw
