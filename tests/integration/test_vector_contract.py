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
