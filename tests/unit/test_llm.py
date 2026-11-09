"""LLM 传输测试，全部离线：模拟传输注入，不联网。"""

from __future__ import annotations

import asyncio
import math

import httpx
import pytest

from libref.config import LibrefSettings
from libref.llm import (
    ChatClient,
    ChatConfig,
    ChatResult,
    CircuitBreaker,
    EmbeddingClient,
    EmbeddingConfig,
    EmbeddingResult,
    ImageRef,
    LlmCircuitOpenError,
    LlmConfigError,
    LlmDecodeError,
    LlmError,
    LlmGateway,
    LlmInvalidRequestError,
    LlmProviderError,
    Preprocessor,
    RateLimiter,
    RerankClient,
    RerankConfig,
    Resilience,
    RetryPolicy,
    ToolCall,
    build_chat_fn,
    build_chat_stream_fn,
    build_embed_fn,
    build_rerank_fn,
    embedding_dim_for,
    parse_retry_after_ms,
    truncate_content,
    with_resilience,
)
from libref.llm.retry import execute_with_retry


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
    c = EmbeddingClient(
        base="https://api.siliconflow.cn/v1", api_key="k", model="BAAI/bge-m3", dim=2
    )
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
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [3.0, 4.0]}]}
        )

    c = EmbeddingClient(base="https://b", api_key="k", model="m", dim=2)
    vecs = asyncio.run(c.encode(["x"], client=_client(handler)))
    assert len(calls) == 2
    assert math.isclose(sum(v * v for v in vecs[0]), 1.0, rel_tol=1e-6)


def test_rerank_disabled_by_default(monkeypatch, tmp_path) -> None:
    s = _isolated_settings(
        monkeypatch, tmp_path, embedding_api_key="k", rerank_enabled=False
    )
    assert build_rerank_fn(s) is None


def test_rerank_scores_sorted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/rerank")
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.2},
                    {"index": 1, "relevance_score": 0.9},
                ]
            },
        )

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
    s = _isolated_settings(
        monkeypatch, tmp_path, embedding_api_key="k", chat_enabled=True
    )
    assert build_chat_fn(s) is not None


def test_chat_complete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        body = __import__("json").loads(request.content.decode())
        assert body["stream"] is False
        assert body["enable_thinking"] is False
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hello"}}]}
        )

    c = ChatClient(base="https://b", api_key="k", model="m")
    assert (
        asyncio.run(
            c.complete([{"role": "user", "content": "hi"}], client=_client(handler))
        )
        == "hello"
    )


def test_chat_empty_messages_rejected() -> None:
    c = ChatClient(base="https://b", api_key="k", model="m")
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(c.complete([]))


def test_error_classification() -> None:
    err400 = LlmProviderError(400, "bad")
    assert err400.is_retryable() is False
    assert err400.is_network_failure() is False
    err429 = LlmProviderError(429, "limit", 2000)
    assert err429.is_retryable() is True
    assert err429.retry_after_ms() == 2000
    err500 = LlmProviderError(500, "down")
    assert err500.is_retryable() is True
    assert LlmDecodeError("x").is_retryable() is False
    EmbeddingClient(base="https://b", api_key="k", model="m", dim=2)
    with pytest.raises(ValueError):
        EmbeddingClient(base="https://b", api_key="", model="m", dim=2)


def test_retry_after_parsing() -> None:
    assert parse_retry_after_ms(None) is None
    assert parse_retry_after_ms("") is None
    assert parse_retry_after_ms("2") == 2000
    assert parse_retry_after_ms("nope") is None


def test_retry_policy_floor_and_observer() -> None:
    p = RetryPolicy(max_retries=2, base_delay=0.01)
    assert p.delay_for_attempt(1, 500) >= 0.5
    seen: list = []
    calls: list = []

    async def op():
        calls.append(1)
        if len(calls) < 3:
            raise LlmProviderError(500, "down")
        return "ok"

    out = asyncio.run(
        execute_with_retry(
            p,
            op,
            is_retryable=lambda e: isinstance(e, LlmError) and e.is_retryable(),
            retry_after_ms_of=lambda e: (
                e.retry_after_ms() if isinstance(e, LlmError) else None
            ),
            on_retry=seen.append,
            reason_of=str,
        )
    )
    assert out == "ok"
    assert len(calls) == 3
    assert len(seen) == 2


def test_embedding_duplicate_index_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [1.0, 0.0]},
                    {"index": 0, "embedding": [0.0, 1.0]},
                ]
            },
        )

    c = EmbeddingClient(base="https://b", api_key="k", model="m", dim=2)
    with pytest.raises(LlmDecodeError, match="duplicate"):
        asyncio.run(c.encode(["a", "b"], client=_client(handler)))


def test_embedding_rejected_no_retry() -> None:
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"message": "bad"})

    c = EmbeddingClient(base="https://b", api_key="k", model="m", dim=2)
    with pytest.raises(LlmProviderError) as exc_info:
        asyncio.run(c.encode(["x"], client=_client(handler)))
    assert exc_info.value.is_retryable() is False
    assert len(calls) == 1


def test_rerank_duplicate_and_range_rejected() -> None:
    c = RerankClient(base="https://b", api_key="k", model="r")

    def dup(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 0, "relevance_score": 0.6},
                ]
            },
        )

    with pytest.raises(LlmDecodeError, match="duplicate"):
        asyncio.run(c.score("q", ["a", "b"], client=_client(dup)))

    def far(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 9, "relevance_score": 0.6},
                ]
            },
        )

    with pytest.raises(LlmDecodeError, match="out-of-range"):
        asyncio.run(c.score("q", ["a", "b"], client=_client(far)))


def test_rerank_truncates_long_document() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content.decode())
        seen["len"] = len(body["documents"][0])
        return httpx.Response(
            200, json={"results": [{"index": 0, "relevance_score": 1.0}]}
        )

    c = RerankClient(base="https://b", api_key="k", model="r")
    asyncio.run(c.score("q", ["x" * 600], client=_client(handler)))
    assert seen["len"] == 503
    assert truncate_content("short", 500) == "short"


def test_chat_stream_assembles_deltas() -> None:
    body = (
        'data: {"choices": [{"delta": {"content": "hel"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "lo"}}]}\n\n'
        "data: [DONE]\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content.decode())
        assert payload["stream"] is True
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/event-stream"}
        )

    async def collect() -> str:
        c = ChatClient(base="https://b", api_key="k", model="m")
        out: list[str] = []
        async for chunk in c.stream(
            [{"role": "user", "content": "hi"}], client=_client(handler)
        ):
            out.append(chunk)
        return "".join(out)

    assert asyncio.run(collect()) == "hello"


def test_chat_stream_builder_gating(monkeypatch, tmp_path) -> None:
    s = _isolated_settings(monkeypatch, tmp_path)
    assert build_chat_stream_fn(s) is None
    on = _isolated_settings(
        monkeypatch, tmp_path, embedding_api_key="k", chat_enabled=True
    )
    assert build_chat_stream_fn(on) is not None


def test_chat_invalid_message_rejected() -> None:
    c = ChatClient(base="https://b", api_key="k", model="m")
    with pytest.raises(ValueError, match="role"):
        asyncio.run(c.complete([{"role": "user", "content": ""}]))


def test_config_bridge_gating(monkeypatch, tmp_path) -> None:
    assert EmbeddingConfig.from_settings(
        _isolated_settings(monkeypatch, tmp_path)
    ) is None
    assert RerankConfig.from_settings(
        _isolated_settings(
            monkeypatch, tmp_path, embedding_api_key="k", rerank_enabled=False
        )
    ) is None
    assert ChatConfig.from_settings(
        _isolated_settings(monkeypatch, tmp_path)
    ) is None
    on = _isolated_settings(
        monkeypatch, tmp_path, embedding_api_key="k", chat_enabled=True
    )
    chat = ChatConfig.from_settings(on)
    assert chat is not None
    assert chat.api_key == "k"
    assert chat.base == on.chat_api_base


def test_config_bridge_fails_fast(monkeypatch, tmp_path) -> None:
    with pytest.raises(LlmConfigError, match="dim"):
        EmbeddingConfig.from_settings(
            _isolated_settings(
                monkeypatch, tmp_path, embedding_api_key="k", embedding_dim=0
            )
        )
    with pytest.raises(LlmConfigError, match="base"):
        EmbeddingClient(base="  ", api_key="k", model="m", dim=2)
    with pytest.raises(LlmConfigError, match="base"):
        RerankClient(base="", api_key="k", model="r")
    with pytest.raises(LlmConfigError, match="base"):
        ChatClient(base="", api_key="k", model="m")
    with pytest.raises(LlmConfigError, match="dim"):
        build_embed_fn(
            _isolated_settings(
                monkeypatch, tmp_path, embedding_api_key="k", embedding_dim=0
            )
        )


def test_rate_limiter_burst_passes() -> None:
    async def drain() -> None:
        limiter = RateLimiter(1000.0, 3)
        for _ in range(3):
            await limiter.acquire()

    asyncio.run(drain())
    with pytest.raises(LlmConfigError, match="rate"):
        RateLimiter(0.0, 1)


def test_breaker_opens_and_recovers() -> None:
    async def drive() -> None:
        breaker = CircuitBreaker(failure_threshold=2, cooldown=0.05)
        assert breaker.allow() is True
        breaker.record_failure()
        assert breaker.allow() is True
        breaker.record_failure()
        assert breaker.allow() is False
        await asyncio.sleep(0.06)
        assert breaker.allow() is True
        breaker.record_success()
        assert breaker.allow() is True

    asyncio.run(drive())


def test_breaker_short_circuits_client() -> None:
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500)

    async def drive() -> None:
        breaker = CircuitBreaker(failure_threshold=1, cooldown=60.0)
        res = Resilience(breaker=breaker)
        c = EmbeddingClient(
            base="https://b",
            api_key="k",
            model="m",
            dim=2,
            retries=0,
            resilience=res,
        )
        with pytest.raises(LlmProviderError):
            await c.encode(["x"], client=_client(handler))
        with pytest.raises(LlmCircuitOpenError):
            await c.encode(["x"], client=_client(handler))

    asyncio.run(drive())
    assert len(calls) == 1


def test_with_resilience_passthrough() -> None:
    async def op() -> str:
        return "ok"

    assert asyncio.run(with_resilience(None, op)) == "ok"


def test_preprocessor_modes() -> None:
    assert Preprocessor().apply_batch(["a"]) == ["a"]
    assert Preprocessor("prefix", "q: ").apply("a") == "q: a"
    assert Preprocessor("template", "[{{text}}]").apply("a") == "[a]"
    with pytest.raises(LlmConfigError, match="kind"):
        Preprocessor("suffix", "x")


def test_preprocessor_applied_before_send() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content.decode())
        seen["input"] = body["input"]
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    c = EmbeddingClient(
        base="https://b",
        api_key="k",
        model="m",
        dim=1,
        preprocessor=Preprocessor("prefix", "q: "),
    )
    asyncio.run(c.encode(["a"], client=_client(handler)))
    assert seen["input"] == ["q: a"]


def test_image_input_rejected_without_network() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call network")

    c = EmbeddingClient(base="https://b", api_key="k", model="m", dim=2)
    with pytest.raises(LlmInvalidRequestError, match="image"):
        asyncio.run(c.encode_inputs([ImageRef("https://img/x.png")], client=_client(boom)))


def test_embedding_usage_sums_batches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content.decode())
        n = len(body["input"])
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [1.0, 0.0]} for i in range(n)
                ],
                "usage": {"prompt_tokens": 5, "total_tokens": 7},
            },
        )

    c = EmbeddingClient(
        base="https://b", api_key="k", model="m", dim=2, batch_size=1
    )
    result = asyncio.run(c.encode_with_usage(["a", "b"], client=_client(handler)))
    assert isinstance(result, EmbeddingResult)
    assert len(result.vectors) == 2
    assert result.usage == {"prompt_tokens": 10, "total_tokens": 14}


def test_chat_result_tools_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "done",
                            "tool_calls": [
                                {
                                    "id": "t1",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q": "x"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            },
        )

    c = ChatClient(base="https://b", api_key="k", model="m")
    result = asyncio.run(
        c.complete_result([{"role": "user", "content": "hi"}], client=_client(handler))
    )
    assert isinstance(result, ChatResult)
    assert result.text == "done"
    assert result.tool_calls == [ToolCall("t1", "lookup", {"q": "x"})]
    assert result.usage == {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}


def test_chat_bad_tool_arguments_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "done",
                            "tool_calls": [
                                {
                                    "id": "t1",
                                    "function": {"name": "f", "arguments": "{"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    c = ChatClient(base="https://b", api_key="k", model="m")
    with pytest.raises(LlmDecodeError, match="arguments"):
        asyncio.run(
            c.complete_result(
                [{"role": "user", "content": "hi"}], client=_client(handler)
            )
        )


def test_model_catalog() -> None:
    assert embedding_dim_for("BAAI/bge-m3") == 1024
    assert embedding_dim_for("unknown-model") is None


def test_known_model_dim_mismatch_rejected(monkeypatch, tmp_path) -> None:
    with pytest.raises(LlmConfigError, match="dim mismatch"):
        EmbeddingConfig.from_settings(
            _isolated_settings(
                monkeypatch, tmp_path, embedding_api_key="k", embedding_dim=512
            )
        )


def test_gateway_shares_resilience(monkeypatch, tmp_path) -> None:
    s = _isolated_settings(monkeypatch, tmp_path, embedding_api_key="k")
    gw = LlmGateway(s)
    assert gw.resilience_for("https://a/v1") is gw.resilience_for("https://a/v1/")
    assert gw.resilience_for("https://a/v1") is not gw.resilience_for("https://b/v1")
    assert gw.rerank_fn() is None
    assert gw.chat_fn() is None
    assert gw.embed_fn() is not None


def test_gateway_embed_end_to_end(monkeypatch, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [1.0] * 1024}]}
        )

    s = _isolated_settings(monkeypatch, tmp_path, embedding_api_key="k")
    gw = LlmGateway(s)
    fn = gw.embed_fn(transport=httpx.MockTransport(handler))
    assert fn is not None
    vecs = asyncio.run(fn(["a"]))
    assert len(vecs[0]) == 1024
    assert math.isclose(sum(v * v for v in vecs[0]), 1.0, rel_tol=1e-6)
