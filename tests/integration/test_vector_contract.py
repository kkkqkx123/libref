"""Vector contract tests shared by the simvec and Qdrant backends.

The same cases run on both branches: tenant filtering on search, count and
list_ids, idempotent re-ingest, per-version delete semantics, and the
backend-neutral diagnostics snapshot. The Qdrant branch runs only when a
local service is reachable (marker `requires_qdrant`).
"""

from __future__ import annotations

import os

import pytest

from libref.db import apply_migrations, open_db
from libref.store.vec_store import (
    QdrantVecStore,
    SimvecVecStore,
    close_simvec_engines,
    simvec_available,
)

COLLECTION = "libref_contract_vec"

_QDRANT_URL = os.environ.get("LIBREF_TEST_QDRANT_URL", "")

needs_lib = pytest.mark.skipif(not simvec_available(), reason="libsimvec not built")
needs_qdrant = pytest.mark.skipif(
    _QDRANT_URL == "", reason="LIBREF_TEST_QDRANT_URL not set"
)


def _make_store(backend: str, tmp_path):
    if backend == "simvec":
        return SimvecVecStore(tmp_path / "engine", COLLECTION)
    return QdrantVecStore(_QDRANT_URL, COLLECTION, timeout=5.0)


_backend_params = [
    pytest.param("simvec", marks=needs_lib),
    pytest.param("qdrant", marks=[needs_qdrant, pytest.mark.requires_qdrant]),
]


def _embed4(texts):
    return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def _point(doc_id: int, version_id: int, tenant_id: str | None):
    return (
        doc_id,
        1,
        version_id,
        [1.0, 0.0, 0.0, 0.0],
        tenant_id,
    )


@pytest.fixture()
def conn(tmp_path):
    c = open_db(tmp_path / "vec_contract.db")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def cleanup():
    yield
    close_simvec_engines()


@pytest.mark.integration
@pytest.mark.parametrize("backend", _backend_params)
def test_tenant_filter_and_idempotent_upsert(backend, conn, tmp_path, cleanup):
    store = _make_store(backend, tmp_path)
    store.ensure_collection(conn, "test/model", 4)
    store.upsert_batch([_point(1, 10, "default"), _point(2, 10, "acme")], wait=True)
    # Idempotent replay by document id: the point count does not grow.
    store.upsert_batch([_point(1, 10, "default")], wait=True)

    assert store.count(10) >= 1
    assert store.count(10, "default") == 1
    assert store.count(10, "acme") == 1
    assert store.count(10, "other") == 0

    default_ids = store.list_ids(10, tenant_id="default")
    acme_ids = store.list_ids(10, tenant_id="acme")
    assert 1 in default_ids and 1 not in acme_ids
    assert 2 in acme_ids and 2 not in default_ids

    hits = store.search([1.0, 0.0, 0.0, 0.0], k=10, version_id=10, tenant_id="acme")
    assert [h[0] for h in hits] == [2]


@pytest.mark.integration
@pytest.mark.parametrize("backend", _backend_params)
def test_delete_version_scoped_by_tenant(backend, conn, tmp_path, cleanup):
    store = _make_store(backend, tmp_path)
    store.ensure_collection(conn, "test/model", 4)
    store.upsert_batch(
        [_point(1, 20, "default"), _point(2, 20, "acme"), _point(3, 21, "default")],
        wait=True,
    )
    store.delete_version(20, "acme")
    assert store.count(20, "acme") == 0
    assert store.count(20, "default") == 1
    assert store.count(21) == 1

    store.delete_version(20)
    assert store.count(20) == 0
    assert store.count(21) == 1


@pytest.mark.integration
@pytest.mark.parametrize("backend", _backend_params)
def test_dimension_change_rebuilds_collection(backend, conn, tmp_path, cleanup):
    store = _make_store(backend, tmp_path)
    store.ensure_collection(conn, "test/model", 4)
    store.upsert_batch([_point(1, 30, "default")], wait=True)
    assert store.diagnostics()["collection_exists"] is True
    # Dimension change forces a collection rebuild; existing points are dropped.
    store.ensure_collection(conn, "test/model", 8)
    assert store.count(30) == 0


@pytest.mark.integration
@pytest.mark.parametrize("backend", _backend_params)
def test_diagnostics_snapshot_shape(backend, conn, tmp_path, cleanup):
    store = _make_store(backend, tmp_path)
    store.ensure_collection(conn, "test/model", 4)
    snap = store.diagnostics()
    assert snap["backend"] == backend
    assert snap["collection"] == COLLECTION
    assert snap["reachable"] is True
    assert snap["collection_exists"] is True
    assert isinstance(snap.get("points", 0), int)


def _profile_store(backend: str, tmp_path, *, max_points=None, **shape):
    """Store bound to an explicit collection profile; the profile shapes the collection."""
    from libref.store.vec_store import VectorProfile

    profile = VectorProfile(backend=backend, **shape)
    if backend == "simvec":
        return SimvecVecStore(
            tmp_path / "engine", COLLECTION, profile=profile, max_points=max_points
        )
    return QdrantVecStore(
        _QDRANT_URL, COLLECTION, timeout=5.0, profile=profile, max_points=max_points
    )


@pytest.mark.integration
@pytest.mark.parametrize("backend", _backend_params)
def test_profile_change_rebuilds_collection(backend, conn, tmp_path, cleanup):
    first = _profile_store(backend, tmp_path, distance="cosine")
    first.ensure_collection(conn, "test/model", 4)
    first.upsert_batch([_point(1, 41, "default")], wait=True)
    assert first.count(41) == 1

    # Distance is part of the collection shape, so a change rebuilds and drops points.
    changed = _profile_store(backend, tmp_path, distance="dot")
    changed.ensure_collection(conn, "test/model", 4)
    assert changed.count(41) == 0
    assert changed.diagnostics()["profile"]["distance"] == "dot"


@pytest.mark.integration
@pytest.mark.parametrize("backend", _backend_params)
def test_model_change_keeps_collection(backend, conn, tmp_path, cleanup):
    store = _profile_store(backend, tmp_path)
    store.ensure_collection(conn, "model-a", 4)
    store.upsert_batch([_point(2, 42, "default")], wait=True)

    # Model drift is repaired per version by maintenance, never by wiping the collection.
    store.ensure_collection(conn, "model-b", 4)
    assert store.count(42) == 1


@pytest.mark.integration
@pytest.mark.parametrize("backend", _backend_params)
def test_payload_index_replay(backend, conn, tmp_path, cleanup):
    store = _profile_store(backend, tmp_path)
    store.ensure_collection(conn, "test/model", 4)
    assert {"version_id", "library_id", "tenant_id"} <= set(
        store.diagnostics()["payload_indexes"]
    )

    # A collection that lost a filter index is repaired on the next governance pass.
    if backend == "simvec":
        store._engine.delete_payload_index(COLLECTION, "tenant_id")
    else:
        store.client.delete_payload_index(COLLECTION, "tenant_id", wait=True)
    store.ensure_collection(conn, "test/model", 4)
    assert "tenant_id" in store.diagnostics()["payload_indexes"]


@pytest.mark.integration
@pytest.mark.parametrize("backend", _backend_params)
def test_capacity_gate_refuses_overflow(backend, conn, tmp_path, cleanup):
    from libref.store.vec_store import VectorCapacityError

    store = _profile_store(backend, tmp_path, max_points=2)
    store.ensure_collection(conn, "test/model", 4)
    store.upsert_batch([_point(1, 43, "default"), _point(2, 43, "default")], wait=True)
    assert store.count(43) == 2

    # The ceiling is a hard stop: the index is derived, so the batch stays replayable.
    with pytest.raises(VectorCapacityError, match="vector_max_points"):
        store.upsert_batch([_point(3, 43, "default")], wait=True)
    assert store.count(43) == 2


@pytest.mark.integration
@pytest.mark.requires_qdrant
@needs_qdrant
def test_remote_tuning_applies_without_rebuild(conn, tmp_path, cleanup):
    first = _profile_store(
        "qdrant", tmp_path, qdrant_hnsw_m=32, qdrant_hnsw_ef_construct=200
    )
    first.ensure_collection(conn, "test/model", 4)
    first.upsert_batch([_point(1, 44, "default")], wait=True)
    live = first.client.get_collection(COLLECTION).config.hnsw_config
    assert (live.m, live.ef_construct) == (32, 200)

    # Tuning lives in its own segment, so graph drift rewrites the live collection.
    tuned = _profile_store(
        "qdrant", tmp_path, qdrant_hnsw_m=48, qdrant_hnsw_ef_construct=300
    )
    tuned.ensure_collection(conn, "test/model", 4)
    assert tuned.count(44) == 1
    live = tuned.client.get_collection(COLLECTION).config.hnsw_config
    assert (live.m, live.ef_construct) == (48, 300)


@pytest.mark.integration
@pytest.mark.requires_qdrant
@needs_qdrant
def test_remote_shape_drift_rebuilds(conn, tmp_path, cleanup):
    first = _profile_store("qdrant", tmp_path, qdrant_shard_number=1)
    first.ensure_collection(conn, "test/model", 4)
    first.upsert_batch([_point(1, 45, "default")], wait=True)
    assert first.count(45) == 1

    # Shard count is creation-only, so it can only be answered by a rebuild.
    reshaped = _profile_store("qdrant", tmp_path, qdrant_shard_number=2)
    reshaped.ensure_collection(conn, "test/model", 4)
    assert reshaped.count(45) == 0
    assert reshaped.client.get_collection(COLLECTION).config.params.shard_number == 2
