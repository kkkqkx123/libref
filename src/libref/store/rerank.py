"""重排传输，蓝本 tools/hurl/rerank.hurl；纯内存输入输出，不碰数据库。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

RerankFn = Callable[[str, list[str], int | None], Awaitable[list[tuple[int, float]]]]
"""重排函数：查询加文档列表加截断数进，索引与分数列表出，分数降序。"""


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
    ) -> None:
        if not api_key:
            raise ValueError("rerank api key is empty")
        if not model:
            raise ValueError("rerank model is empty")
        self._base = base.rstrip("/")
        self._key = api_key
        self._model = model
        self._timeout = timeout
        self._retries = retries

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
            raise ValueError("rerank query is empty")
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
        body: dict = {
            "model": self._model,
            "query": query,
            "documents": documents,
            "return_documents": False,
        }
        if top_n is not None:
            body["top_n"] = top_n
        error: str | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = await client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                error = f"HTTP {resp.status_code}"
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"rerank rejected: HTTP {resp.status_code}")
            payload = resp.json()
            items = payload.get("results")
            if not isinstance(items, list):
                raise RuntimeError("rerank response missing results")
            out: list[tuple[int, float]] = []
            for item in items:
                if "relevance_score" in item:
                    score = float(item["relevance_score"])
                elif "score" in item:
                    score = float(item["score"])
                else:
                    raise RuntimeError("rerank result missing score")
                out.append((int(item["index"]), score))
            out.sort(key=lambda kv: kv[1], reverse=True)
            return out
        raise RuntimeError(f"rerank failed: {error}")


def build_rerank_fn(settings, transport=None):
    """按配置构造重排函数，未启用、无模型或无密钥时返回空。"""
    if not getattr(settings, "rerank_enabled", False):
        return None
    model = getattr(settings, "rerank_model", "")
    if not model:
        return None
    api_key = getattr(settings, "embedding_api_key", None)
    if not api_key:
        return None
    client = RerankClient(
        base=getattr(settings, "embedding_api_base", ""),
        api_key=api_key,
        model=model,
        timeout=getattr(settings, "request_timeout", 30.0),
    )
    if transport is None:
        return client.score
    owned_transport = transport

    async def rerank(
        query: str, documents: list[str], top_n: int | None = None
    ) -> list[tuple[int, float]]:
        async with httpx.AsyncClient(
            timeout=getattr(settings, "request_timeout", 30.0),
            transport=owned_transport,
        ) as c:
            return await client.score(query, documents, top_n, client=c)

    return rerank
