"""Hybrid retrieval offline goldens: FakeVec injection, no Qdrant or embedding API dependency."""

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
        (
            "pattern",
            "https://d.example/pattern.html",
            "quetzalhybrid numpy.array numpy.zeros workflow body",
        ),
    ):
        r = store.store_page(
            1,
            1,
            url,
            "stored",
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


def test_multi_version_fanout_capped(conn, tmp_path, monkeypatch) -> None:
    class _VersionVec:
        def __init__(self) -> None:
            self.seen: list[int] = []

        def search(self, query_vec, *, k: int, version_id: int):
            self.seen.append(version_id)
            return []

    conn.execute("INSERT INTO libraries(name) VALUES('docs')")
    for i in range(1, 5):
        conn.execute(
            "INSERT INTO versions(id, library_id, version) VALUES(?, 1, ?)",
            (i, f"{i}.0"),
        )
    conn.commit()
    monkeypatch.chdir(tmp_path)
    s = LibrefSettings(retrieval_max_versions=2)
    vec = _VersionVec()
    _, degraded = asyncio.run(
        HybridRetriever(conn, s, vec_store=vec, embed=_fake_embed).search_async(
            "quetzalcap", limit=2
        )
    )
    assert degraded is True
    assert vec.seen == [1, 2]


def test_hybrid_marks_sources_and_fused_score_sorted(
    conn, tmp_path, monkeypatch
) -> None:
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
    rrf_order = rec._rrf([ids["plain"], ids["pattern"]], [ids["plain"], ids["pattern"]])
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
    assert next(r["id"] for r in results) == ids["pattern"]


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


def test_precise_attribution_ignores_substring(conn, tmp_path, monkeypatch) -> None:
    seed_library_version(conn)
    store = DocumentStore(conn)
    hit = store.store_page(
        1,
        1,
        "https://d.example/exact.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/exact.html",
                path="G",
                title="G",
                content="quetzalgate numpy.array usage",
                sort_order=0,
            )
        ],
        raw_content="quetzalgate numpy.array usage",
    )
    miss = store.store_page(
        1,
        1,
        "https://d.example/sub.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/sub.html",
                path="G",
                title="G",
                content="quetzalgate my_numpy.arrayish helper",
                sort_order=0,
            )
        ],
        raw_content="quetzalgate my_numpy.arrayish helper",
    )
    conn.execute(
        "INSERT INTO seq_patterns(library_id, version_id, pattern, support, is_closed)"
        " VALUES(1, 1, ?, 9, 1)",
        (json.dumps(["numpy.array"]),),
    )
    conn.commit()
    monkeypatch.chdir(tmp_path)
    s = LibrefSettings(freq_alpha=5.0, cluster_alpha=0.0)
    vec = _FakeVec([miss.inserted_ids[0], hit.inserted_ids[0]])
    results, _ = asyncio.run(
        HybridRetriever(conn, s, vec_store=vec, embed=_fake_embed).search_async(
            "quetzalgate", limit=8
        )
    )
    first = next(r["id"] for r in results)
    assert first == hit.inserted_ids[0]


def test_dual_track_gate_records_both_roads(conn, tmp_path, monkeypatch) -> None:
    ids = _seed(conn)
    monkeypatch.chdir(tmp_path)
    s = LibrefSettings(freq_alpha=5.0, cluster_alpha=0.0)
    vec = _FakeVec([ids["pattern"], ids["plain"]])
    fts_only, _ = asyncio.run(
        HybridRetriever(conn, s).search_async("quetzalhybrid", limit=8)
    )
    hybrid, degraded = asyncio.run(
        HybridRetriever(conn, s, vec_store=vec, embed=_fake_embed).search_async(
            "quetzalhybrid", limit=8
        )
    )
    assert degraded is False
    assert len(fts_only) == 2 and len(hybrid) == 2
    assert next(r["id"] for r in hybrid) == ids["pattern"]
    assert all(isinstance(r["score"], float) for r in hybrid)


def _mrr_and_recall(
    ranked_ids: list[int], relevant: set[int], k: int
) -> tuple[float, float]:
    rank = next((i + 1 for i, doc in enumerate(ranked_ids) if doc in relevant), 0)
    mrr = 1.0 / rank if rank else 0.0
    hits = len(set(ranked_ids[:k]).intersection(relevant))
    recall = hits / len(relevant) if relevant else 1.0
    return mrr, recall


def test_quality_gate_mrr_recall_no_regression(conn, tmp_path, monkeypatch) -> None:
    seed_library_version(conn)
    store = DocumentStore(conn)
    ids: dict[str, int] = {}
    for key, body in (
        ("install", "quetzalalpha install guide apple orchard"),
        ("advanced", "quetzalalpha advanced usage banana grove"),
        ("quick", "quetzalalpha quickstart apple banana meadow"),
        ("other", "unrelated pineapple desert"),
    ):
        url = f"https://d.example/{key}.html"
        result = store.store_page(
            1,
            1,
            url,
            "stored",
            chunks=[
                Chunk(url=url, path="Guide", title=key, content=body, sort_order=0)
            ],
            raw_content=body,
        )
        ids[key] = result.inserted_ids[0]
    cases = [
        ("apple install orchard", {ids["install"]}),
        ("banana advanced grove", {ids["advanced"]}),
        ("quickstart meadow", {ids["quick"]}),
    ]
    monkeypatch.chdir(tmp_path)
    settings = LibrefSettings(freq_alpha=0.0, cluster_alpha=0.0)
    fts_mrrs: list[float] = []
    fts_recalls: list[float] = []
    for query, relevant in cases:
        results, degraded = asyncio.run(
            HybridRetriever(conn, settings).search_async(query, limit=4)
        )
        assert degraded is False
        mrr, recall = _mrr_and_recall([r["id"] for r in results], relevant, 2)
        fts_mrrs.append(mrr)
        fts_recalls.append(recall)
    fts_mrr = sum(fts_mrrs) / len(fts_mrrs)
    fts_recall = sum(fts_recalls) / len(fts_recalls)
    assert fts_mrr >= 0.5
    assert fts_recall >= 0.66
    helpful = _FakeVec([ids["install"], ids["advanced"], ids["quick"], ids["other"]])
    hybrid_mrrs: list[float] = []
    hybrid_recalls: list[float] = []
    for query, relevant in cases:
        results, degraded = asyncio.run(
            HybridRetriever(
                conn, settings, vec_store=helpful, embed=_fake_embed
            ).search_async(query, limit=4)
        )
        assert degraded is False
        mrr, recall = _mrr_and_recall([r["id"] for r in results], relevant, 2)
        hybrid_mrrs.append(mrr)
        hybrid_recalls.append(recall)
    hybrid_mrr = sum(hybrid_mrrs) / len(hybrid_mrrs)
    hybrid_recall = sum(hybrid_recalls) / len(hybrid_recalls)
    assert hybrid_mrr >= 0.5
    assert hybrid_recall >= 0.66
    assert hybrid_mrr >= fts_mrr
    assert hybrid_recall >= fts_recall
