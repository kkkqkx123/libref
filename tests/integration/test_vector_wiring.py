"""Settings-to-store wiring: profile, collection name, write batch and search knobs.

These cases pin the seam between configuration and the vector backends: a setting
that never reaches the store is indistinguishable from one that does not exist.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from libref.services.factory import build_vector
from libref.store.vec_store import (
    QdrantVecStore,
    SimvecVecStore,
    close_simvec_engines,
    simvec_available,
)
from tests.conftest import make_settings

_QDRANT_URL = os.environ.get("LIBREF_TEST_QDRANT_URL", "")

needs_lib = pytest.mark.skipif(not simvec_available(), reason="libsimvec not built")
needs_qdrant = pytest.mark.skipif(
    _QDRANT_URL == "", reason="LIBREF_TEST_QDRANT_URL not set"
)


def _gateway():
    async def _embed(texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    return SimpleNamespace(embed_fn=lambda: _embed)


@pytest.mark.integration
@pytest.mark.requires_simvec
@needs_lib
def test_simvec_settings_reach_store(tmp_path, monkeypatch) -> None:
    settings = make_settings(
        monkeypatch,
        tmp_path,
        db_path=tmp_path / "wired.db",
        vector_backend="simvec",
        simvec_dir=tmp_path / "engine",
        vector_collection="wired",
        deployment_id="prod-a",
        vector_write_batch=64,
        vector_distance="euclid",
        simvec_index="flat",
        simvec_hnsw_ef_construct=100,
    )
    vec, _, _ = build_vector(settings, gateway=_gateway())
    try:
        assert isinstance(vec, SimvecVecStore)
        assert vec.profile.distance == "euclid"
        assert vec.profile.index == "flat"
        assert vec.write_batch == 64
        assert vec.collection.startswith("wired_")

        # The declared deployment identity decides the name, not the database file.
        same_deployment = make_settings(
            monkeypatch,
            tmp_path,
            db_path=tmp_path / "elsewhere.db",
            vector_backend="simvec",
            simvec_dir=tmp_path / "engine",
            vector_collection="wired",
            deployment_id="prod-a",
        )
        other, _, _ = build_vector(same_deployment, gateway=_gateway())
        assert other.collection == vec.collection
    finally:
        close_simvec_engines()


@pytest.mark.integration
@pytest.mark.requires_qdrant
@needs_qdrant
def test_qdrant_settings_reach_store(tmp_path, monkeypatch) -> None:
    settings = make_settings(
        monkeypatch,
        tmp_path,
        db_path=tmp_path / "remote.db",
        vector_backend="qdrant",
        qdrant_url=_QDRANT_URL,
        vector_collection="wired_remote",
        vector_distance="dot",
        vector_write_batch=32,
        qdrant_search_hnsw_ef=64,
    )
    vec, _, _ = build_vector(settings, gateway=_gateway())
    assert isinstance(vec, QdrantVecStore)
    # Local graph knobs never leak into a remote profile: only backend and distance count.
    assert vec.profile.identity() == {"backend": "qdrant", "distance": "dot"}
    assert vec.search_hnsw_ef == 64
    assert vec.search_exact is False
    assert vec.write_batch == 32
