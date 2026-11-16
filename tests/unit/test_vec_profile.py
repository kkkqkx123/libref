"""Unit tests for the collection profile: what changes its fingerprint and what must not."""

from __future__ import annotations

from libref.store.vec_store import VectorProfile, collection_for_deployment


def test_identity_covers_distance_and_local_index_shape() -> None:
    profile = VectorProfile(backend="simvec", distance="cosine", index="hnsw")
    identity = profile.identity()
    assert identity["backend"] == "simvec"
    assert identity["distance"] == "cosine"
    assert identity["index"] == "hnsw"
    assert identity["hnsw"] == {
        "m": 16,
        "ef_construct": 100,
        "full_scan_threshold": 10000,
        "ef_search": 40,
    }


def test_flat_tier_has_no_graph_parameters() -> None:
    flat = VectorProfile(backend="simvec", index="flat")
    assert "hnsw" not in flat.identity()


def test_remote_identity_ignores_local_graph_knobs() -> None:
    # Tuning the local engine must not rebuild a remote collection.
    left = VectorProfile(backend="qdrant", hnsw_m=16)
    right = VectorProfile(backend="qdrant", hnsw_m=64)
    assert left.fingerprint() == right.fingerprint()
    assert left.identity() == {"backend": "qdrant", "distance": "cosine"}


def test_fingerprint_tracks_shape_changes() -> None:
    base = VectorProfile(backend="simvec", distance="cosine", index="hnsw")
    assert (
        base.fingerprint()
        == VectorProfile(
            backend="simvec", distance="cosine", index="hnsw"
        ).fingerprint()
    )
    assert (
        base.fingerprint()
        != VectorProfile(backend="simvec", distance="dot", index="hnsw").fingerprint()
    )
    assert (
        base.fingerprint()
        != VectorProfile(
            backend="simvec", distance="cosine", index="flat"
        ).fingerprint()
    )
    assert (
        base.fingerprint()
        != VectorProfile(
            backend="simvec", distance="cosine", index="hnsw", hnsw_ef_construct=200
        ).fingerprint()
    )


def test_max_indexing_threads_only_appears_when_set() -> None:
    unset = VectorProfile(backend="simvec")
    threaded = VectorProfile(backend="simvec", max_indexing_threads=4)
    assert "max_indexing_threads" not in unset.identity()["hnsw"]
    assert threaded.identity()["hnsw"]["max_indexing_threads"] == 4


def test_for_backend_keeps_shape_but_rebinds_identity() -> None:
    profile = VectorProfile(backend="simvec", distance="euclid").for_backend("qdrant")
    assert profile.backend == "qdrant"
    assert profile.distance == "euclid"


def test_collection_name_follows_deployment_identity(tmp_path) -> None:
    db_path = tmp_path / "libref.db"
    named = collection_for_deployment("libref_chunks", "prod-a", db_path)
    assert named.startswith("libref_chunks_")
    # The same deployment identifier always derives the same collection, so
    # replicas behind one vector service share it regardless of their db path.
    assert named == collection_for_deployment(
        "libref_chunks", "prod-a", tmp_path / "x.db"
    )
    assert named != collection_for_deployment("libref_chunks", "prod-b", db_path)
    # Without a declared identity the file identity decides.
    assert collection_for_deployment("libref_chunks", "", db_path).startswith(
        "libref_chunks_"
    )
    assert collection_for_deployment("libref_chunks", "", db_path) != named


def test_remote_tuning_changes_only_the_tuning_segment() -> None:
    base = VectorProfile(backend="qdrant", qdrant_hnsw_m=32)
    tuned = VectorProfile(backend="qdrant", qdrant_hnsw_m=48)
    # Graph parameters are rewritten in place, so they must not force a rebuild.
    assert base.fingerprint() == tuned.fingerprint()
    assert base.tuning_fingerprint() != tuned.tuning_fingerprint()
    assert base.tuning_identity() == {"hnsw_m": 32}


def test_remote_shape_changes_the_rebuild_fingerprint() -> None:
    base = VectorProfile(backend="qdrant", qdrant_shard_number=1)
    wider = VectorProfile(backend="qdrant", qdrant_shard_number=2)
    # Shard and replica counts only exist at creation time, so they rebuild.
    assert base.fingerprint() != wider.fingerprint()
    assert wider.shape_identity()["shard_number"] == 2
    assert base.tuning_identity() == {}


def test_remote_unset_fields_keep_service_defaults() -> None:
    # None means "whatever the service decides", so it stays out of the identity.
    profile = VectorProfile(backend="qdrant")
    assert profile.shape_identity() == {"backend": "qdrant", "distance": "cosine"}
    assert profile.tuning_identity() == {}
    assert profile.identity() == {"backend": "qdrant", "distance": "cosine"}


def test_local_engine_has_no_tuning_segment() -> None:
    # The local engine fixes graph parameters at creation, so they belong to the shape.
    profile = VectorProfile(backend="simvec", index="hnsw", hnsw_m=32)
    assert profile.tuning_identity() == {}
    assert profile.shape_identity()["hnsw"]["m"] == 32
    assert profile.shape_identity() == profile.identity()
