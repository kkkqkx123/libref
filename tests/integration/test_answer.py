"""QA orchestration tests: preset retrieval plus grounded generation, all offline."""

from __future__ import annotations

import httpx
import pytest

from libref.chunking import Chunk
from libref.llm import LlmConfigError, build_chat_fn
from libref.services.docs.answer import answer_query
from libref.store.document_store import DocumentStore
from tests.conftest import make_settings, seed_library_version

pytestmark = pytest.mark.integration


def _seed_docs(conn) -> None:
    seed_library_version(conn)
    store = DocumentStore(conn)
    store.store_page(
        1,
        1,
        "https://d.example/a.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/a.html",
                path="G",
                title="G",
                content="libref uses uv for deps",
                sort_order=0,
            )
        ],
        raw_content="libref uses uv for deps",
    )


def _chat_fn(settings, handler):
    fn = build_chat_fn(settings, transport=httpx.MockTransport(handler))
    assert fn is not None
    return fn


def test_answer_grounds_in_retrieved_material(conn, monkeypatch, tmp_path) -> None:
    _seed_docs(conn)
    s = make_settings(monkeypatch, tmp_path, embedding_api_key="k", chat_enabled=True)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content.decode())
        seen["user"] = body["messages"][-1]["content"]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "用 uv 管理依赖 [1]"}}]}
        )

    out = answer_query(conn, s, "uv", chat=_chat_fn(s, handler))
    assert out["answer"] == "用 uv 管理依赖 [1]"
    assert len(out["citations"]) == 1
    cite = out["citations"][0]
    assert cite["url"] == "https://d.example/a.html"
    assert cite["title"] == "G"
    assert cite["library"] == "docs"
    assert cite["version"] == "1.0"
    assert cite["page_id"] == 1
    assert cite["sort_order"] == 0
    assert out["degraded"] is False
    assert out["reasons"] == []
    assert out["model"] == s.chat_model
    assert out["confidence"] == "high"
    assert out["cited_ids"] == [cite["id"]]
    assert "Materials" in seen["user"]
    assert "https://d.example/a.html" in seen["user"]


def test_answer_without_citation_marks_low_confidence(
    conn, monkeypatch, tmp_path
) -> None:
    _seed_docs(conn)
    s = make_settings(monkeypatch, tmp_path, embedding_api_key="k", chat_enabled=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "用 uv 管理依赖"}}]}
        )

    out = answer_query(conn, s, "uv", chat=_chat_fn(s, handler))
    assert out["answer"] == "用 uv 管理依赖"
    assert out["citations"] == []
    assert out["cited_ids"] == []
    assert out["confidence"] == "low"


def test_answer_empty_without_evidence(conn, monkeypatch, tmp_path) -> None:
    _seed_docs(conn)
    s = make_settings(monkeypatch, tmp_path, embedding_api_key="k", chat_enabled=True)

    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call chat without evidence")

    out = answer_query(conn, s, "pineapplezzzqqq", chat=_chat_fn(s, boom))
    assert out["answer"] == ""
    assert out["citations"] == []
    assert out["degraded"] is False
    assert out["reasons"] == []
    assert out["model"] == ""
    assert out["confidence"] == "low"
    assert out["cited_ids"] == []


def test_answer_rejects_when_chat_disabled(conn, monkeypatch, tmp_path) -> None:
    _seed_docs(conn)
    s = make_settings(monkeypatch, tmp_path)
    with pytest.raises(LlmConfigError, match="chat is not enabled"):
        answer_query(conn, s, "uv")


def test_answer_rejects_empty_query(conn, monkeypatch, tmp_path) -> None:
    _seed_docs(conn)
    s = make_settings(monkeypatch, tmp_path, embedding_api_key="k", chat_enabled=True)
    with pytest.raises(ValueError, match="empty"):
        answer_query(conn, s, "  ")


def test_answer_rejects_unknown_preset(conn, monkeypatch, tmp_path) -> None:
    _seed_docs(conn)
    s = make_settings(monkeypatch, tmp_path, embedding_api_key="k", chat_enabled=True)
    with pytest.raises(ValueError, match="preset"):
        answer_query(conn, s, "uv", preset="turbo")


def _seed_entity_docs(conn) -> None:
    seed_library_version(conn)
    store = DocumentStore(conn)
    store.store_page(
        1,
        1,
        "https://d.example/numpy.html",
        "stored",
        chunks=[
            Chunk(
                url="https://d.example/numpy.html",
                path="G",
                title="G",
                content="numpy.array creates an array, numpy.zeros workflow",
                sort_order=0,
                entities=["numpy.array"],
            )
        ],
        raw_content="numpy.array creates an array, numpy.zeros workflow",
    )
    import json as _json

    conn.execute(
        "INSERT INTO pattern_labels(library_id, version_id, pattern_hash, pattern,"
        " model, prompt_version, name, summary, workflow, related_apis)"
        " VALUES(1, 1, 'h', ?, 't', 'v1', 'make', 'make arrays', 'do', ?)",
        (_json.dumps(["numpy.array", "numpy.zeros"]), _json.dumps(["numpy.array"])),
    )
    conn.commit()


def _cited_chat(settings, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content.decode())
        seen["user"] = body["messages"][-1]["content"]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "调用 array [1]"}}]}
        )

    return _chat_fn(settings, handler)


def test_answer_precise_skips_pattern_labels(conn, monkeypatch, tmp_path) -> None:
    _seed_entity_docs(conn)
    s = make_settings(monkeypatch, tmp_path, embedding_api_key="k", chat_enabled=True)
    seen_balanced: dict = {}
    out = answer_query(
        conn, s, "numpy", chat=_cited_chat(s, seen_balanced), preset="balanced"
    )
    assert out["confidence"] == "high"
    assert "label-1" in seen_balanced["user"]
    seen_precise: dict = {}
    out = answer_query(
        conn, s, "numpy", chat=_cited_chat(s, seen_precise), preset="precise"
    )
    assert out["confidence"] == "high"
    assert "label-1" not in seen_precise["user"]
