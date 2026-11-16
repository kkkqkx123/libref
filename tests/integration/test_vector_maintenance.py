"""Explicit vector maintenance: publishing the graph on demand instead of on a tick.

The local engine promotes to HNSW from a background worker that ticks every 30 s, so
a freshly loaded collection serves exact scans for up to a tick. ``optimize`` closes
that window synchronously; these cases pin the threshold rule that decides whether a
graph is worth publishing at all.
"""

from __future__ import annotations

import os

import pytest

from libref.db import apply_migrations, open_db
from libref.store.vec_store import (
    QdrantVecStore,
    SimvecVecStore,
    VectorProfile,
    close_simvec_engines,
    simvec_available,
)

COLLECTION = "libref_maintenance_vec"
DIM = 8

_QDRANT_URL = os.environ.get("LIBREF_TEST_QDRANT_URL", "")

needs_lib = pytest.mark.skipif(not simvec_available(), reason="libsimvec not built")
needs_qdrant = pytest.mark.skipif(
    _QDRANT_URL == "", reason="LIBREF_TEST_QDRANT_URL not set"
)


@pytest.fixture()
def conn(tmp_path):
    c = open_db(tmp_path / "vec_maintenance.db")
    apply_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def cleanup():
    yield
    close_simvec_engines()


def _points(count: int) -> list[tuple[int, int, int, list[float]]]:
    return [
        (doc_id, 1, 60, [float(doc_id % 7)] + [1.0, 0.0, 0.5, 0.2, 0.1, 0.0, 0.3])
        for doc_id in range(1, count + 1)
    ]


def _simvec(tmp_path, **profile):
    return SimvecVecStore(
        tmp_path / "engine",
        COLLECTION,
        profile=VectorProfile(backend="simvec", **profile),
        write_batch=256,
    )


@pytest.mark.integration
@needs_lib
def test_optimize_publishes_graph_above_threshold(conn, tmp_path, cleanup):
    store = _simvec(tmp_path, index="hnsw", full_scan_threshold=5)
    store.ensure_collection(conn, "test/model", DIM)
    store.upsert_batch(_points(10))

    # Nothing is published yet: the engine only builds on its own tick.
    assert store.diagnostics()["has_index"] is False
    out = store.optimize()
    assert out["index_built"] is True
    assert out["has_index"] is True
    assert store.search([1.0, 1.0, 0.0, 0.5, 0.2, 0.1, 0.0, 0.3], k=3, version_id=60)


@pytest.mark.integration
@needs_lib
def test_optimize_keeps_exact_scan_below_threshold(conn, tmp_path, cleanup):
    store = _simvec(tmp_path, index="hnsw", full_scan_threshold=10_000)
    store.ensure_collection(conn, "test/model", DIM)
    store.upsert_batch(_points(10))

    # Below the threshold a graph costs more than it saves, so none is forced.
    out = store.optimize()
    assert out["index_built"] is False
    assert out["has_index"] is False
    assert store.search([1.0, 1.0, 0.0, 0.5, 0.2, 0.1, 0.0, 0.3], k=3, version_id=60)


@pytest.mark.integration
@needs_lib
def test_optimize_never_builds_for_flat_tier(conn, tmp_path, cleanup):
    store = _simvec(tmp_path, index="flat", full_scan_threshold=5)
    store.ensure_collection(conn, "test/model", DIM)
    store.upsert_batch(_points(10))

    out = store.optimize()
    assert out["index_built"] is False
    assert out["has_index"] is False


@pytest.mark.integration
@needs_lib
def test_optimize_skips_absent_collection(conn, tmp_path, cleanup):
    store = _simvec(tmp_path, index="hnsw")
    # An unprovisioned collection is a first-run state, not an error.
    out = store.optimize()
    assert out["skipped"] == "collection does not exist"


@pytest.mark.integration
@needs_qdrant
def test_optimize_applies_remote_tuning(conn, tmp_path, cleanup):
    store = QdrantVecStore(
        _QDRANT_URL,
        COLLECTION,
        timeout=5.0,
        profile=VectorProfile(
            backend="qdrant", qdrant_hnsw_m=48, qdrant_hnsw_ef_construct=300
        ),
    )
    store.ensure_collection(conn, "test/model", DIM)
    store.upsert_batch(_points(3))

    # The service owns graph publication, so the report names that instead of faking it.
    out = store.optimize()
    assert out["graph"] == "service-managed"
    assert out["points"] == 3
    live = store.client.get_collection(COLLECTION).config.hnsw_config
    assert (live.m, live.ef_construct) == (48, 300)
