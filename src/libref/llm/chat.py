"""Chat transport, compatible with non-streaming and streaming chat endpoints; disabled by default, not wired into the retrieval call chain."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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

ChatFn = Callable[[list[dict[str, str]]], Awaitable[str]]
"""Chat function: message list in, text out."""

ChatStreamFn = Callable[[list[dict[str, str]]], AsyncIterator[str]]
"""Streaming chat function: message list in, text increments out."""


@dataclass(frozen=True)
class ToolCall:
    """Tool call: identifier plus name plus resolved arguments."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatResult:
    """Chat result; text, tool calls, and usage on the same record."""

    text: str
    tool_calls: list[ToolCall]
    usage: dict[str, int]


def _tool_calls_of(message: dict[str, Any]) -> list[ToolCall]:
    raw = message.get("tool_calls") or []
    if not isinstance(raw, list):
        raise LlmDecodeError("chat response tool_calls is not a list")
    out: list[ToolCall] = []
    for item in raw:
        fn = item.get("function", {}) if isinstance(item, dict) else {}
        name = fn.get("name") if isinstance(fn, dict) else None
        args_raw = fn.get("arguments", "{}") if isinstance(fn, dict) else "{}"
        if not isinstance(item, dict) or not item.get("id") or not name:
            raise LlmDecodeError("chat tool call missing id or name")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except ValueError as exc:
            raise LlmDecodeError(
                f"chat tool call arguments are not objects: {exc}"
            ) from exc
        if not isinstance(args, dict):
            raise LlmDecodeError("chat tool call arguments are not objects")
        out.append(ToolCall(str(item["id"]), str(name), args))
    return out


def _usage_of(payload: dict[str, Any]) -> dict[str, int]:
    raw = payload.get("usage") if isinstance(payload, dict) else None
    source = raw if isinstance(raw, dict) else {}
    return {
        "prompt_tokens": int(source.get("prompt_tokens", 0)),
        "completion_tokens": int(source.get("completion_tokens", 0)),
        "total_tokens": int(source.get("total_tokens", 0)),
    }


@dataclass(frozen=True)
class ChatConfig:
    """Chat endpoint config; key/base falling back to the embedding values is decided by the assembly layer."""

    base: str
    api_key: str
    model: str
    timeout: float = 60.0
    retries: int = 2
    enable_thinking: bool = False

    def validate(self) -> None:
        if not self.api_key:
            raise LlmConfigError("chat api key is empty")
        if not self.base.strip():
            raise LlmConfigError("chat base is empty")
        if not self.model:
            raise LlmConfigError("chat model is empty")

    @classmethod
    def from_settings(cls, settings: LibrefSettings) -> ChatConfig | None:
        """Return None when not enabled, no model, or no key; key/base fall back to the embedding values."""
        if not settings.chat_enabled:
            return None
        if not settings.chat_model:
            return None
        api_key = settings.chat_api_key or settings.embedding_api_key
        if not api_key:
            return None
        config = cls(
            base=settings.chat_api_base or settings.embedding_api_base,
            api_key=api_key,
            model=settings.chat_model,
            timeout=settings.chat_timeout,
            enable_thinking=settings.chat_enable_thinking,
        )
        config.validate()
        return config


def _check_messages(messages: list[dict[str, str]]) -> None:
    if not messages:
        raise LlmInvalidRequestError("chat messages are empty")
    for item in messages:
        if not item.get("role") or not item.get("content"):
            raise LlmInvalidRequestError("chat message role and content are required")


class ChatClient:
    """Chat-compatible interface; non-streaming is retryable, streaming is not."""

    def __init__(
        self,
        *,
        base: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        retries: int = 2,
        enable_thinking: bool = False,
        retry_policy: RetryPolicy | None = None,
        on_retry: Callable[[RetryAttempt], None] | None = None,
        resilience: Resilience | None = None,
    ) -> None:
        if not api_key:
            raise LlmConfigError("chat api key is empty")
        if not base.strip():
            raise LlmConfigError("chat base is empty")
        if not model:
            raise LlmConfigError("chat model is empty")
        self._base = base.rstrip("/")
        self._key = api_key
        self._model = model
        self._timeout = timeout
        self._policy = (
            retry_policy
            if retry_policy is not None
            else RetryPolicy(max_retries=retries)
        )
        self._on_retry = on_retry
        self._resilience = resilience
        self._thinking = enable_thinking

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> str:
        """Non-streaming generation; empty messages rejected outright without a call."""
        result = await self.complete_result(messages, client=client)
        return result.text

    async def complete_result(
        self,
        messages: list[dict[str, str]],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> ChatResult:
        """Non-streaming generation surfacing tool calls and usage."""
        _check_messages(messages)
        if client is not None:
            return await self._post(client, messages)
        async with httpx.AsyncClient(timeout=self._timeout) as owned:
            return await self._post(owned, messages)

    async def _post(
        self, client: httpx.AsyncClient, messages: list[dict[str, str]]
    ) -> ChatResult:
        url = self._base + "/chat/completions"
        headers = {"Authorization": f"Bearer {self._key}"}
        body = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "enable_thinking": self._thinking,
        }

        async def _once() -> ChatResult:
            return await with_resilience(self._resilience, _send_once)

        async def _send_once() -> ChatResult:
            try:
                resp = await client.post(url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                raise LlmTimeoutError() from exc
            except httpx.HTTPError as exc:
                raise LlmTransportError(f"provider transport failure: {exc}") from exc
            if resp.status_code == 429 or resp.status_code >= 500:
                raise LlmProviderError(
                    resp.status_code,
                    f"chat failed: HTTP {resp.status_code}",
                    parse_retry_after_ms(resp.headers.get("retry-after")),
                )
            if resp.status_code >= 400:
                raise LlmProviderError(
                    resp.status_code, f"chat rejected: HTTP {resp.status_code}"
                )
            try:
                payload = resp.json()
            except ValueError as exc:
                raise LlmDecodeError(f"chat response missing choices: {exc}") from exc
            choices = payload.get("choices")
            if not choices:
                raise LlmDecodeError("chat response missing choices")
            message = choices[0].get("message", {})
            content = message.get("content")
            if not content:
                raise LlmDecodeError("chat response missing content")
            return ChatResult(str(content), _tool_calls_of(message), _usage_of(payload))

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

    async def stream(
        self,
        messages: list[dict[str, str]],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> AsyncIterator[str]:
        """Streaming generation, yielding text incrementally; empty messages rejected outright."""
        _check_messages(messages)
        if client is not None:
            async for chunk in self._stream_with(client, messages):
                yield chunk
            return
        async with httpx.AsyncClient(timeout=self._timeout) as owned:
            async for chunk in self._stream_with(owned, messages):
                yield chunk

    async def _stream_with(
        self, client: httpx.AsyncClient, messages: list[dict[str, str]]
    ) -> AsyncIterator[str]:
        url = self._base + "/chat/completions"
        headers = {"Authorization": f"Bearer {self._key}"}
        body = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "enable_thinking": self._thinking,
        }
        if self._resilience is not None:
            await self._resilience.before_send()
        try:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise LlmProviderError(
                        resp.status_code,
                        f"chat failed: HTTP {resp.status_code}",
                        parse_retry_after_ms(resp.headers.get("retry-after")),
                    )
                if resp.status_code >= 400:
                    raise LlmProviderError(
                        resp.status_code, f"chat rejected: HTTP {resp.status_code}"
                    )
                async for line in resp.aiter_lines():
                    text = line.strip()
                    if not text or text.startswith(":"):
                        continue
                    if not text.startswith("data:"):
                        continue
                    data = text[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                    except ValueError as exc:
                        raise LlmDecodeError(
                            f"chat stream missing choices: {exc}"
                        ) from exc
                    choices = payload.get("choices")
                    if not choices:
                        continue
                    first = choices[0]
                    delta = first.get("delta", {})
                    if not isinstance(delta, dict) or "content" not in delta:
                        delta = first.get("message", {})
                    content = delta.get("content") if isinstance(delta, dict) else None
                    if content:
                        yield str(content)
        except LlmError as exc:
            if self._resilience is not None:
                self._resilience.after_failure(exc)
            raise
        except httpx.TimeoutException as exc:
            err: LlmError = LlmTimeoutError()
            if self._resilience is not None:
                self._resilience.after_failure(err)
            raise err from exc
        except httpx.HTTPError as exc:
            err = LlmTransportError(f"provider transport failure: {exc}")
            if self._resilience is not None:
                self._resilience.after_failure(err)
            raise err from exc
        if self._resilience is not None:
            self._resilience.after_success()


def build_chat_fn(
    settings: LibrefSettings, transport: httpx.BaseTransport | None = None
):
    """Build the chat function from settings; returns None when not enabled, no model, or no key."""
    config = ChatConfig.from_settings(settings)
    if config is None:
        return None
    client = ChatClient(
        base=config.base,
        api_key=config.api_key,
        model=config.model,
        timeout=config.timeout,
        enable_thinking=config.enable_thinking,
    )
    if transport is None:
        return client.complete
    owned_transport = transport

    async def chat(messages: list[dict[str, str]]) -> str:
        async with httpx.AsyncClient(
            timeout=config.timeout,
            transport=owned_transport,
        ) as c:
            return await client.complete(messages, client=c)

    return chat


def build_chat_stream_fn(
    settings: LibrefSettings, transport: httpx.BaseTransport | None = None
):
    """Build the streaming chat function from settings; returns None when not enabled, no model, or no key."""
    config = ChatConfig.from_settings(settings)
    if config is None:
        return None
    client = ChatClient(
        base=config.base,
        api_key=config.api_key,
        model=config.model,
        timeout=config.timeout,
        enable_thinking=config.enable_thinking,
    )
    if transport is None:

        async def stream(messages: list[dict[str, str]]) -> AsyncIterator[str]:
            async for chunk in client.stream(messages):
                yield chunk

        return stream
    owned_transport = transport

    async def stream_with_transport(
        messages: list[dict[str, str]],
    ) -> AsyncIterator[str]:
        async with httpx.AsyncClient(
            timeout=config.timeout,
            transport=owned_transport,
        ) as c:
            async for chunk in client.stream(messages, client=c):
                yield chunk

    return stream_with_transport
