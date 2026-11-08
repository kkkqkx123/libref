"""问答传输，兼容问答端点非流式生成；默认禁用，不接检索调用链。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

ChatFn = Callable[[list[dict[str, str]]], Awaitable[str]]
"""问答函数：消息列表进，文本出。"""


class ChatClient:
    """兼容问答接口，失败直接抛错，不静默回退。"""

    def __init__(
        self,
        *,
        base: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        retries: int = 2,
        enable_thinking: bool = False,
    ) -> None:
        if not api_key:
            raise ValueError("chat api key is empty")
        if not model:
            raise ValueError("chat model is empty")
        self._base = base.rstrip("/")
        self._key = api_key
        self._model = model
        self._timeout = timeout
        self._retries = retries
        self._thinking = enable_thinking

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> str:
        """非流式生成，空消息直接拒绝，不发起调用。"""
        if not messages:
            raise ValueError("chat messages are empty")
        if client is not None:
            return await self._post(client, messages)
        async with httpx.AsyncClient(timeout=self._timeout) as owned:
            return await self._post(owned, messages)

    async def _post(
        self, client: httpx.AsyncClient, messages: list[dict[str, str]]
    ) -> str:
        url = self._base + "/chat/completions"
        headers = {"Authorization": f"Bearer {self._key}"}
        body = {"model": self._model, "messages": messages, "stream": False,
                "enable_thinking": self._thinking}
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
                raise RuntimeError(f"chat rejected: HTTP {resp.status_code}")
            payload = resp.json()
            choices = payload.get("choices")
            if not choices:
                raise RuntimeError("chat response missing choices")
            content = choices[0].get("message", {}).get("content")
            if not content:
                raise RuntimeError("chat response missing content")
            return str(content)
        raise RuntimeError(f"chat failed: {error}")


def build_chat_fn(settings, transport=None):
    """按配置构造问答函数，未启用、无模型或无密钥时返回空。"""
    if not getattr(settings, "chat_enabled", False):
        return None
    model = getattr(settings, "chat_model", "")
    if not model:
        return None
    api_key = getattr(settings, "chat_api_key", None) or getattr(
        settings, "embedding_api_key", None
    )
    if not api_key:
        return None
    base = getattr(settings, "chat_api_base", "") or getattr(
        settings, "embedding_api_base", ""
    )
    client = ChatClient(
        base=base,
        api_key=api_key,
        model=model,
        timeout=getattr(settings, "chat_timeout", 30.0),
        enable_thinking=getattr(settings, "chat_enable_thinking", False),
    )
    if transport is None:
        return client.complete
    owned_transport = transport

    async def chat(messages: list[dict[str, str]]) -> str:
        async with httpx.AsyncClient(
            timeout=getattr(settings, "chat_timeout", 30.0),
            transport=owned_transport,
        ) as c:
            return await client.complete(messages, client=c)

    return chat
