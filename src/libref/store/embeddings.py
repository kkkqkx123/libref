"""编码传输，蓝本 tools/hurl/silicon.hurl；纯内存输入输出，不碰数据库与向量索引。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

EMBED_BATCH = 64

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]
"""批量编码函数：文本列表进，向量列表出，顺序对应。"""


class EmbeddingClient:
    """兼容接口批量编码，超时重试与归一内聚在本层。"""

    def __init__(
        self,
        *,
        base: str,
        api_key: str,
        model: str,
        dim: int,
        timeout: float = 30.0,
        batch_size: int = EMBED_BATCH,
        retries: int = 3,
    ) -> None:
        if not api_key:
            raise ValueError("embedding api key is empty")
        if not model:
            raise ValueError("embedding model is empty")
        if dim <= 0:
            raise ValueError("embedding dim must be positive")
        self._base = base.rstrip("/")
        self._key = api_key
        self._model = model
        self._dim = dim
        self._timeout = timeout
        self._batch = max(1, batch_size)
        self._retries = retries

    async def encode(
        self, texts: list[str], *, client: httpx.AsyncClient | None = None
    ) -> list[list[float]]:
        """批量编码，空输入直接返回空，不发起调用。"""
        if not texts:
            return []
        if client is not None:
            out: list[list[float]] = []
            for i in range(0, len(texts), self._batch):
                out.extend(await self._encode_batch(client, texts[i:i + self._batch]))
            return out
        async with httpx.AsyncClient(timeout=self._timeout) as owned:
            return await self.encode(texts, client=owned)

    async def _encode_batch(
        self, client: httpx.AsyncClient, texts: list[str]
    ) -> list[list[float]]:
        url = self._base + "/embeddings"
        headers = {"Authorization": f"Bearer {self._key}"}
        body = {"model": self._model, "input": texts}
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
                raise RuntimeError(f"embedding rejected: HTTP {resp.status_code}")
            items = sorted(resp.json()["data"], key=lambda d: d["index"])
            vecs: list[list[float]] = []
            for item in items:
                vec = item["embedding"]
                norm = sum(x * x for x in vec) ** 0.5
                vec = [x / norm for x in vec] if norm > 0 else vec
                if self._dim and len(vec) != self._dim:
                    raise RuntimeError(
                        f"embedding dim mismatch: got {len(vec)}, want {self._dim}"
                    )
                vecs.append(vec)
            if len(vecs) != len(texts):
                raise RuntimeError("embedding count mismatch")
            return vecs
        raise RuntimeError(f"embedding failed: {error}")


def build_embed_fn(settings, transport=None) -> EmbedFn | None:
    """按配置构造编码函数，无密钥时返回空，调用方走纯全文路。"""
    api_key = getattr(settings, "embedding_api_key", None)
    if not api_key:
        return None
    client = EmbeddingClient(
        base=getattr(settings, "embedding_api_base", ""),
        api_key=api_key,
        model=getattr(settings, "embedding_model", ""),
        dim=getattr(settings, "embedding_dim", 0),
        timeout=getattr(settings, "request_timeout", 30.0),
    )
    if transport is None:
        return client.encode
    owned_transport = transport

    async def embed(texts: list[str]) -> list[list[float]]:
        async with httpx.AsyncClient(
            timeout=getattr(settings, "request_timeout", 30.0),
            transport=owned_transport,
        ) as c:
            return await client.encode(texts, client=c)

    return embed
