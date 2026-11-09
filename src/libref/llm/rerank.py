"""重排传输，蓝本 tools/hurl/rerank.hurl 与 llm-suite-rerank；纯内存输入输出。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from libref.config import LibrefSettings

from libref.llm.errors import (
    LlmConfigError,
    LlmDecodeError,
    LlmError,
    LlmInvalidRequestError,
    LlmProviderError,
    LlmTimeoutError,
    LlmTransportError,
)
from libref.llm.resilience import Resilience, with_resilience
from libref.llm.retry import (
    RetryAttempt,
    RetryPolicy,
    execute_with_retry,
    parse_retry_after_ms,
)

RerankFn = Callable[[str, list[str], int | None], Awaitable[list[tuple[int, float]]]]
"""重排函数：查询加文档列表加截断数进，索引与分数列表出，分数降序。"""

RERANK_TRUNCATE = 500


def truncate_content(text: str, limit: int = RERANK_TRUNCATE) -> str:
    """按字符安全截断超长文档，截断处标记省略。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


@dataclass(frozen=True)
class RerankConfig:
    """重排端点配置，基址密钥默认复用编码同值由装配层决定。"""

    base: str
    api_key: str
    model: str
    timeout: float = 30.0
    retries: int = 3
    truncate: int = RERANK_TRUNCATE

    def validate(self) -> None:
        if not self.api_key:
            raise LlmConfigError("rerank api key is empty")
        if not self.base.strip():
            raise LlmConfigError("rerank base is empty")
        if not self.model:
            raise LlmConfigError("rerank model is empty")

    @classmethod
    def from_settings(cls, settings: LibrefSettings) -> RerankConfig | None:
        """未启用、无模型或无密钥时返回空；基址密钥复用编码同值。"""
        if not settings.rerank_enabled:
            return None
        if not settings.rerank_model:
            return None
        if not settings.embedding_api_key:
            return None
        config = cls(
            base=settings.embedding_api_base,
            api_key=settings.embedding_api_key,
            model=settings.rerank_model,
            timeout=settings.request_timeout,
            truncate=settings.rerank_truncate,
        )
        config.validate()
        return config


class RerankClient:
    """兼容重排接口，超时重试内聚在本层，失败直接抛错。"""

    def __init__(
        self,
        *,
        base: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        retries: int = 3,
        truncate: int = RERANK_TRUNCATE,
        retry_policy: RetryPolicy | None = None,
        on_retry: Callable[[RetryAttempt], None] | None = None,
        resilience: Resilience | None = None,
    ) -> None:
        if not api_key:
            raise LlmConfigError("rerank api key is empty")
        if not base.strip():
            raise LlmConfigError("rerank base is empty")
        if not model:
            raise LlmConfigError("rerank model is empty")
        self._base = base.rstrip("/")
        self._key = api_key
        self._model = model
        self._timeout = timeout
        self._truncate = truncate
        self._policy = (
            retry_policy
            if retry_policy is not None
            else RetryPolicy(max_retries=retries)
        )
        self._on_retry = on_retry
        self._resilience = resilience

    async def score(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[tuple[int, float]]:
        """按查询打分，空文档直接返回空，不发起调用。"""
        if not documents:
            return []
        if not query.strip():
            raise LlmInvalidRequestError("rerank query is empty")
        if client is not None:
            return await self._post(client, query, documents, top_n)
        async with httpx.AsyncClient(timeout=self._timeout) as owned:
            return await self._post(owned, query, documents, top_n)

    async def _post(
        self,
        client: httpx.AsyncClient,
        query: str,
        documents: list[str],
        top_n: int | None,
    ) -> list[tuple[int, float]]:
        url = self._base + "/rerank"
        headers = {"Authorization": f"Bearer {self._key}"}
        truncated = [truncate_content(d, self._truncate) for d in documents]
        body: dict = {
            "model": self._model,
            "query": query,
            "documents": truncated,
            "top_n": top_n if top_n is not None else len(truncated),
            "return_documents": False,
        }

        async def _once() -> list[tuple[int, float]]:
            return await with_resilience(self._resilience, _send_once)

        async def _send_once() -> list[tuple[int, float]]:
            try:
                resp = await client.post(url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                raise LlmTimeoutError() from exc
            except httpx.HTTPError as exc:
                raise LlmTransportError(f"provider transport failure: {exc}") from exc
            if resp.status_code == 429 or resp.status_code >= 500:
                raise LlmProviderError(
                    resp.status_code,
                    f"rerank failed: HTTP {resp.status_code}",
                    parse_retry_after_ms(resp.headers.get("retry-after")),
                )
            if resp.status_code >= 400:
                raise LlmProviderError(
                    resp.status_code, f"rerank rejected: HTTP {resp.status_code}"
                )
            try:
                payload = resp.json()
            except ValueError as exc:
                raise LlmDecodeError(f"rerank response missing results: {exc}") from exc
            items = payload.get("results")
            if not isinstance(items, list):
                raise LlmDecodeError("rerank response missing results")
            if len(items) != len(documents):
                raise LlmDecodeError(
                    f"rerank count mismatch: got {len(items)}, want {len(documents)}"
                )
            seen: set[int] = set()
            out: list[tuple[int, float]] = []
            for item in items:
                if "relevance_score" in item:
                    score = float(item["relevance_score"])
                elif "score" in item:
                    score = float(item["score"])
                else:
                    raise LlmDecodeError("rerank result missing score")
                idx = int(item["index"])
                if idx in seen:
                    raise LlmDecodeError(f"rerank response duplicate index '{idx}'")
                if idx < 0 or idx >= len(documents):
                    raise LlmDecodeError(f"rerank response out-of-range index '{idx}'")
                seen.add(idx)
                out.append((idx, score))
            out.sort(key=lambda kv: kv[1], reverse=True)
            return out

        return await execute_with_retry(
            self._policy,
            _once,
            is_retryable=lambda e: isinstance(e, LlmError) and e.is_retryable(),
            retry_after_ms_of=lambda e: (
                e.retry_after_ms() if isinstance(e, LlmError) else None
            ),
            on_retry=self._on_retry,
            reason_of=str,
        )


def build_rerank_fn(
    settings: LibrefSettings, transport: httpx.BaseTransport | None = None
):
    """按配置构造重排函数，未启用、无模型或无密钥时返回空。"""
    config = RerankConfig.from_settings(settings)
    if config is None:
        return None
    client = RerankClient(
        base=config.base,
        api_key=config.api_key,
        model=config.model,
        timeout=config.timeout,
        truncate=config.truncate,
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
