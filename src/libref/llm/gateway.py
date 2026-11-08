"""统一装配，蓝本 llm-suite-gateway；按端点共享弹性状态，短命令与长服务共用。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from libref.config import LibrefSettings

from libref.llm.chat import ChatClient, ChatConfig, ChatFn, ChatStreamFn
from libref.llm.embeddings import EmbeddingClient, EmbeddingConfig, EmbedFn
from libref.llm.rerank import RerankClient, RerankConfig, RerankFn
from libref.llm.resilience import CircuitBreaker, RateLimiter, Resilience
from libref.llm.retry import RetryAttempt


class LlmGateway:
    """长生命周期的统一装配点，按端点地址共享熔断限流。

    易错的装配决策只收拢在各配置的桥接方法内，本层只做字段映射与共享。
    """

    def __init__(
        self,
        settings: LibrefSettings,
        *,
        on_retry: Callable[[RetryAttempt], None] | None = None,
        breaker_kwargs: dict[str, Any] | None = None,
        rate_per_sec: float | None = None,
        burst: int = 8,
    ) -> None:
        self._settings = settings
        self._on_retry = on_retry
        self._breaker_kwargs = breaker_kwargs if breaker_kwargs is not None else {}
        self._rate = rate_per_sec
        self._burst = burst
        self._shared: dict[str, Resilience] = {}

    def resilience_for(self, base: str) -> Resilience:
        """取该端点的共享保护件，限流未配置时只共享熔断。"""
        key = base.rstrip("/")
        slot = self._shared.get(key)
        if slot is None:
            limiter = (
                RateLimiter(self._rate, self._burst) if self._rate is not None else None
            )
            slot = Resilience(
                limiter=limiter, breaker=CircuitBreaker(**self._breaker_kwargs)
            )
            self._shared[key] = slot
        return slot

    def embed_fn(self, transport: httpx.BaseTransport | None = None) -> EmbedFn | None:
        """编码函数，无密钥时返回空，调用方走纯全文路。"""
        config = EmbeddingConfig.from_settings(self._settings)
        if config is None:
            return None
        client = EmbeddingClient(
            base=config.base,
            api_key=config.api_key,
            model=config.model,
            dim=config.dim,
            timeout=config.timeout,
            preprocessor=config.preprocessor,
            on_retry=self._on_retry,
            resilience=self.resilience_for(config.base),
        )
        if transport is None:
            return client.encode
        owned_transport = transport

        async def embed(texts: list[str]) -> list[list[float]]:
            async with httpx.AsyncClient(
                timeout=config.timeout,
                transport=owned_transport,
            ) as c:
                return await client.encode(texts, client=c)

        return embed

    def rerank_fn(self, transport: httpx.BaseTransport | None = None) -> RerankFn | None:
        """重排函数，未启用、无模型或无密钥时返回空。"""
        config = RerankConfig.from_settings(self._settings)
        if config is None:
            return None
        client = RerankClient(
            base=config.base,
            api_key=config.api_key,
            model=config.model,
            timeout=config.timeout,
            on_retry=self._on_retry,
            resilience=self.resilience_for(config.base),
        )
        if transport is None:
            return client.score
        owned_transport = transport

        async def rerank(
            query: str, documents: list[str], top_n: int | None = None
        ) -> list[tuple[int, float]]:
            async with httpx.AsyncClient(
                timeout=config.timeout,
                transport=owned_transport,
            ) as c:
                return await client.score(query, documents, top_n, client=c)

        return rerank

    def _chat_client(self) -> ChatClient | None:
        config = ChatConfig.from_settings(self._settings)
        if config is None:
            return None
        return ChatClient(
            base=config.base,
            api_key=config.api_key,
            model=config.model,
            timeout=config.timeout,
            enable_thinking=config.enable_thinking,
            on_retry=self._on_retry,
            resilience=self.resilience_for(config.base),
        )

    def chat_fn(self, transport: httpx.BaseTransport | None = None) -> ChatFn | None:
        """问答函数，未启用、无模型或无密钥时返回空。"""
        client = self._chat_client()
        if client is None:
            return None
        if transport is None:
            return client.complete
        owned_transport = transport
        timeout = self._settings.chat_timeout

        async def chat(messages: list[dict[str, str]]) -> str:
            async with httpx.AsyncClient(
                timeout=timeout,
                transport=owned_transport,
            ) as c:
                return await client.complete(messages, client=c)

        return chat

    def chat_stream_fn(
        self, transport: httpx.BaseTransport | None = None
    ) -> ChatStreamFn | None:
        """问答流函数，未启用、无模型或无密钥时返回空。"""
        client = self._chat_client()
        if client is None:
            return None
        if transport is None:
            return client.stream
        owned_transport = transport
        timeout = self._settings.chat_timeout

        async def stream(
            messages: list[dict[str, str]],
        ) -> AsyncIterator[str]:
            async with httpx.AsyncClient(
                timeout=timeout,
                transport=owned_transport,
            ) as c:
                async for chunk in client.stream(messages, client=c):
                    yield chunk

        return stream
