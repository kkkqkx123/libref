"""问答编排测试：混合检索加 grounded 生成，全部离线。"""

from __future__ import annotations

import httpx
import pytest

from libref.chunking import Chunk
from libref.config import LibrefSettings
from libref.llm import LlmConfigError, build_chat_fn
from libref.services.answer import answer_query
from libref.store.document_store import DocumentStore


def _isolated_settings(monkeypatch, tmp_path, **kwargs) -> LibrefSettings:
    monkeypatch.chdir(tmp_path)
    return LibrefSettings(**kwargs)


def _seed_docs(conn) -> None:
    conn.execute("INSERT INTO libraries(id, name) VALUES(1, 'docs')")
    conn.execute("INSERT INTO versions(id, library_id, version) VALUES(1, 1, '1.0')")
    conn.commit()
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
    s = _isolated_settings(
        monkeypatch, tmp_path, embedding_api_key="k", chat_enabled=True
    )
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content.decode())
        seen["user"] = body["messages"][-1]["content"]
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "用 uv 管理依赖"}}]}
        )

    out = answer_query(conn, s, "uv", chat=_chat_fn(s, handler))
    assert out["answer"] == "用 uv 管理依赖"
    assert out["citations"] == [
        {"url": "https://d.example/a.html", "title": "G"}
    ]
    assert out["degraded"] is False
    assert out["model"] == s.chat_model
    assert "材料" in seen["user"]
    assert "https://d.example/a.html" in seen["user"]


def test_answer_empty_without_evidence(conn, monkeypatch, tmp_path) -> None:
    _seed_docs(conn)
    s = _isolated_settings(
        monkeypatch, tmp_path, embedding_api_key="k", chat_enabled=True
    )

    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call chat without evidence")

    out = answer_query(
        conn, s, "pineapplezzzqqq", chat=_chat_fn(s, boom)
    )
    assert out == {"answer": "", "citations": [], "degraded": False, "model": ""}


def test_answer_rejects_when_chat_disabled(conn, monkeypatch, tmp_path) -> None:
    _seed_docs(conn)
    s = _isolated_settings(monkeypatch, tmp_path)
    with pytest.raises(LlmConfigError, match="chat is not enabled"):
        answer_query(conn, s, "uv")


def test_answer_rejects_empty_query(conn, monkeypatch, tmp_path) -> None:
    _seed_docs(conn)
    s = _isolated_settings(
        monkeypatch, tmp_path, embedding_api_key="k", chat_enabled=True
    )
    with pytest.raises(ValueError, match="empty"):
        answer_query(conn, s, "  ")
