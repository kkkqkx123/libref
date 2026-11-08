"""LLM 传输与混合检索测试，全部离线：模拟传输注入，不联网。"""

from __future__ import annotations

import asyncio
import math

import httpx
import pytest

from libref.chunking import Chunk
from libref.config import LibrefSettings
from libref.services.search import SearchService
from libref.store.chat import ChatClient, build_chat_fn
from libref.store.document_store import DocumentStore
from libref.store.embeddings import EmbeddingClient, build_embed_fn
from libref.store.rerank import RerankClient, build_rerank_fn
from libref.store.retriever import HybridRetriever


def _isolated_settings(monkeypatch, tmp_path, **kwargs) -> LibrefSettings:
    monkeypatch.chdir(tmp_path)
    return LibrefSettings(**kwargs)


def test_new_defaults(monkeypatch, tmp_path) -> None:
    s = _isolated_settings(monkeypatch, tmp_path)
    assert s.rerank_model == "BAAI/bge-reranker-v2-m3"
    assert s.rerank_enabled is False
    assert s.rerank_candidate_limit == 30
    assert s.rerank_top_n == 10
    assert s.rrf_k == 20
    assert s.chat_model == "Qwen/Qwen3.5-4B"
    assert s.chat_enabled is False
    assert s.chat_api_key is None


def test_builders_skip_without_key(monkeypatch, tmp_path) -> None:
    s = _isolated_settings(monkeypatch, tmp_path)
    assert s.embedding_api_key is None
    assert build_embed_fn(s) is None
    assert build_rerank_fn(s) is None
    assert build_chat_fn(s) is None


def _embed_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path.endswith("/embeddings")
    body = __import__("json").loads(request.content.decode())
    assert body["model"] == "BAAI/bge-m3"
    assert isinstance(body["input"], list)
    data = [
        {"index": i, "embedding": [float(i + 1), 0.0]}
        for i in range(len(body["input"]))
    ]
    return httpx.Response(200, json={"data": list(reversed(data))})


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_embedding_roundtrip_sorted_and_normalized() -> None:
    c = EmbeddingClient(base="https://api.siliconflow.cn/v1", api_key="k",
                        model="BAAI/bge-m3", dim=2)
    vecs = asyncio.run(c.encode(["a", "b"], client=_client(_embed_handler)))
    assert len(vecs) == 2
    assert math.isclose(vecs[0][0], 1.0, rel_tol=1e-6)
    assert math.isclose(vecs[1][0], 1.0, rel_tol=1e-6)


def test_embedding_empty_makes_no_request() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call network")

    c = EmbeddingClient(base="https://b", api_key="k", model="m", dim=2)
    assert asyncio.run(c.encode([], client=_client(boom))) == []


def test_embedding_dim_mismatch_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    c = EmbeddingClient(base="https://b", api_key="k", model="m", dim=2)
    with pytest.raises(RuntimeError, match="dim mismatch"):
        asyncio.run(c.encode(["x"], client=_client(handler)))


def test_embedding_retries_429_then_succeeds() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [3.0, 4.0]}]})

    c = EmbeddingClient(base="https://b", api_key="k", model="m", dim=2)
    vecs = asyncio.run(c.encode(["x"], client=_client(handler)))
    assert len(calls) == 2
    assert math.isclose(sum(v * v for v in vecs[0]), 1.0, rel_tol=1e-6)


def test_rerank_disabled_by_default(monkeypatch, tmp_path) -> None:
    s = _isolated_settings(monkeypatch, tmp_path,
                           embedding_api_key="k", rerank_enabled=False)
    assert build_rerank_fn(s) is None


def test_rerank_scores_sorted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rerank")
        return httpx.Response(200, json={"results": [
            {"index": 0, "relevance_score": 0.2},
            {"index": 1, "relevance_score": 0.9},
        ]})

    c = RerankClient(base="https://b", api_key="k", model="r")
    out = asyncio.run(c.score("q", ["d0", "d1"], client=_client(handler)))
    assert out == [(1, 0.9), (0, 0.2)]


def test_rerank_empty_docs_no_request() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call network")

    c = RerankClient(base="https://b", api_key="k", model="r")
    assert asyncio.run(c.score("q", [], client=_client(boom))) == []


def test_rerank_missing_results_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    c = RerankClient(base="https://b", api_key="k", model="r")
    with pytest.raises(RuntimeError, match="missing results"):
        asyncio.run(c.score("q", ["d"], client=_client(handler)))


def test_chat_disabled_by_default(monkeypatch, tmp_path) -> None:
    s = _isolated_settings(monkeypatch, tmp_path)
    assert build_chat_fn(s) is None


def test_chat_builder_uses_embedding_key_fallback(monkeypatch, tmp_path) -> None:
    s = _isolated_settings(monkeypatch, tmp_path,
                           embedding_api_key="k", chat_enabled=True)
    assert build_chat_fn(s) is not None


def test_chat_complete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        body = __import__("json").loads(request.content.decode())
        assert body["stream"] is False
        assert body["enable_thinking"] is False
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"}}]})

    c = ChatClient(base="https://b", api_key="k", model="m")
    assert asyncio.run(c.complete([{"role": "user", "content": "hi"}],
                                  client=_client(handler))) == "hello"


def test_chat_empty_messages_rejected() -> None:
    c = ChatClient(base="https://b", api_key="k", model="m")
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(c.complete([]))


class _FakeVec:
    def __init__(self, hits: list[int]) -> None:
        self._hits = hits
        self.searched: list[int] = []

    def search(self, query_vec, *, k: int, version_id: int):
        self.searched.append(version_id)
        return [(d, 1.0 - i * 0.1) for i, d in enumerate(self._hits[:k])]

    def ensure_collection(self, conn, model: str, dim: int) -> None:
        pass

    def delete_version(self, version_id: int) -> None:
        pass

    def upsert_batch(self, points, *, wait: bool = True) -> None:
        self.upserted = list(points)


def _seed_docs(conn) -> list[int]:
    conn.execute("INSERT INTO libraries(id, name) VALUES(1, 'docs')")
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, '1.0')")
    conn.commit()
    store = DocumentStore(conn)
    ids: list[int] = []
    for url, body in (("https://d.example/a.html", "libref uses uv for deps"),
                      ("https://d.example/b.html", "unrelated pineapple text")):
        r = store.store_page(
            1, 1, url, "stored",
            chunks=[Chunk(url=url, path="G", title="G", content=body, sort_order=0)],
            raw_content=body,
        )
        ids.extend(r.inserted_ids)
    return ids


async def _vec_embed(texts: list[str]) -> list[list[float]]:
    return [[1.0, 0.0] for _ in texts]


def test_retriever_fts_only_without_vec(conn, monkeypatch, tmp_path) -> None:
    _seed_docs(conn)
    s = _isolated_settings(monkeypatch, tmp_path)
    results, degraded = asyncio.run(
        HybridRetriever(conn, s).search_async("libref"))
    assert degraded is False
    assert len(results) == 1
    assert results[0]["url"] == "https://d.example/a.html"
    assert results[0]["sources"] == ["fts"]


def test_retriever_hybrid_marks_both_sources(conn, monkeypatch, tmp_path) -> None:
    ids = _seed_docs(conn)
    s = _isolated_settings(monkeypatch, tmp_path)
    vec = _FakeVec(list(reversed(ids)))
    results, degraded = asyncio.run(
        HybridRetriever(conn, s, vec_store=vec,
                        embed=_vec_embed).search_async("libref"))
    assert degraded is False
    assert vec.searched == [1]
    both = [r for r in results if r["sources"] == ["fts", "vec"]
            or set(r["sources"]) == {"fts", "vec"}]
    assert both


def test_retriever_degraded_on_embed_failure(conn, monkeypatch, tmp_path) -> None:
    _seed_docs(conn)
    s = _isolated_settings(monkeypatch, tmp_path)

    async def boom(texts: list[str]):
        raise RuntimeError("encoder down")

    results, degraded = asyncio.run(
        HybridRetriever(conn, s, vec_store=_FakeVec([1]),
                        embed=boom).search_async("libref"))
    assert degraded is True
    assert len(results) == 1


def test_reembed_single_version(conn, monkeypatch, tmp_path) -> None:
    ids = _seed_docs(conn)
    s = _isolated_settings(monkeypatch, tmp_path)
    vec = _FakeVec([])
    svc = SearchService(conn, s, vec_store=vec, embed=_vec_embed)
    out = asyncio.run(svc.reembed_library_async("docs", "1.0"))
    assert out == {"docs": len(ids), "upserted": len(ids)}
    assert sorted(p[0] for p in vec.upserted) == sorted(ids)
