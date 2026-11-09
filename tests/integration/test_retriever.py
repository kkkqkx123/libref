"""混合检索离线黄金：FakeVec 注入，不依赖 Qdrant 与编码 API。"""

from __future__ import annotations

import asyncio
import json

import pytest

from libref.chunking import Chunk
from libref.config import LibrefSettings
from libref.store.document_store import DocumentStore
from libref.store.retriever import HybridRetriever
from tests.conftest import seed_library_version

pytestmark = pytest.mark.integration


class _FakeVec:
    def __init__(self, hits: list[int]) -> None:
        self._hits = hits

    def search(self, query_vec, *, k: int, version_id: int):
        return [(d, 1.0) for d in self._hits[:k]]


async def _fake_embed(texts: list[str]) -> list[list[float]]:
    return [[1.0, 0.0] for _ in texts]


def _seed(conn) -> dict[str, int]:
    seed_library_version(conn)
    store = DocumentStore(conn)
    ids: dict[str, int] = {}
    for key, url, body in (
        ("plain", "https://d.example/plain.html", "quetzalhybrid plain body text"),
        ("pattern", "https://d.example/pattern.html",
         "quetzalhybrid numpy.array numpy.zeros workflow body"),
    ):
        r = store.store_page(
            1, 1, url, "stored",
            chunks=[Chunk(url=url, path="G", title="G", content=body, sort_order=0)],
            raw_content=body,
        )
        ids[key] = r.inserted_ids[0]
    conn.execute(
        "INSERT INTO seq_patterns(library_id, version_id, pattern, support, is_closed)"
        " VALUES(1, 1, ?, 9, 1)",
        (json.dumps(["numpy.array", "numpy.zeros"]),),
    )
    conn.execute(
        "INSERT INTO seq_patterns(library_id, version_id, pattern, support, is_closed)"
        " VALUES(1, 1, ?, 1, 1)",
        (json.dumps(["unrelated"]),),
    )
    conn.commit()
    return ids


def test_rrf_missing_road_contributes_zero(conn, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    s = LibrefSettings()
    r = HybridRetriever(conn, s)
    assert r._rrf([1], []) == [1]
    assert r._rrf([1, 2], [2, 3]) == [2, 1, 3]


def test_hybrid_marks_sources_and_fused_score_sorted(conn, tmp_path, monkeypatch) -> None:
    ids = _seed(conn)
    monkeypatch.chdir(tmp_path)
    s = LibrefSettings(freq_alpha=0.0, cluster_alpha=0.0)
    vec = _FakeVec([ids["pattern"], ids["plain"]])
    results, degraded = asyncio.run(
        HybridRetriever(conn, s, vec_store=vec, embed=_fake_embed).search_async(
            "quetzalhybrid", limit=8
        )
    )
    assert degraded is False
    assert len(results) == 2
    assert all(set(r["sources"]) == {"fts", "vec"} for r in results)
    assert results[0]["score"] >= results[1]["score"]
    assert isinstance(results[0]["score"], float)


def test_freq_alpha_zero_is_noop(conn, tmp_path, monkeypatch) -> None:
    ids = _seed(conn)
    monkeypatch.chdir(tmp_path)
    s = LibrefSettings(freq_alpha=0.0, cluster_alpha=0.0)
    vec = _FakeVec([ids["plain"], ids["pattern"]])
    rec = HybridRetriever(conn, s, vec_store=vec, embed=_fake_embed)
    rrf_order = rec._rrf([ids["plain"], ids["pattern"]],
                          [ids["plain"], ids["pattern"]])
    results, _ = asyncio.run(rec.search_async("quetzalhybrid", limit=8))
    assert [r["id"] for r in results] == rrf_order


def test_freq_bonus_promotes_pattern_doc(conn, tmp_path, monkeypatch) -> None:
    ids = _seed(conn)
    monkeypatch.chdir(tmp_path)
    s = LibrefSettings(freq_alpha=5.0, cluster_alpha=0.0)
    vec = _FakeVec([ids["plain"], ids["pattern"]])
    results, _ = asyncio.run(
        HybridRetriever(conn, s, vec_store=vec, embed=_fake_embed).search_async(
            "quetzalhybrid", limit=8
        )
    )
    assert [r["id"] for r in results][0] == ids["pattern"]


def test_degraded_on_embed_failure(conn, tmp_path, monkeypatch) -> None:
    _seed(conn)
    monkeypatch.chdir(tmp_path)
    s = LibrefSettings()

    async def boom(texts: list[str]):
        raise RuntimeError("encoder down")

    results, degraded = asyncio.run(
        HybridRetriever(conn, s, vec_store=_FakeVec([1]), embed=boom).search_async(
            "quetzalhybrid"
        )
    )
    assert degraded is True
    assert len(results) == 2


def test_fts_only_without_vec_is_not_degraded(conn, tmp_path, monkeypatch) -> None:
    _seed(conn)
    monkeypatch.chdir(tmp_path)
    s = LibrefSettings()
    results, degraded = asyncio.run(
        HybridRetriever(conn, s).search_async("quetzalhybrid")
    )
    assert degraded is False
    assert len(results) == 2
    assert all(r["sources"] == ["fts"] for r in results)
