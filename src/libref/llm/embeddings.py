"""Embedding transport, blueprints tools/hurl/silicon.hurl and llm-suite-embedding; pure in-memory input/output."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
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
from libref.llm.models import embedding_dim_for
from libref.llm.resilience import Resilience, with_resilience
from libref.llm.retry import (
    RetryAttempt,
    RetryPolicy,
    execute_with_retry,
    parse_retry_after_ms,
)

EMBED_BATCH = 64

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]
"""Batch embedding function: list of texts in, list of vectors out, order preserved."""


@dataclass(frozen=True)
class Preprocessor:
    """Pre-embedding processing: passthrough, prefix, or template; passthrough by default."""

    kind: str = "none"
    param: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ("none", "prefix", "template"):
            raise LlmConfigError(f"unknown preprocessor kind: {self.kind}")

    def apply(self, text: str) -> str:
        if self.kind == "prefix":
            return f"{self.param}{text}"
        if self.kind == "template":
            return self.param.replace("{{text}}", text)
        return text

    def apply_batch(self, texts: list[str]) -> list[str]:
        return [self.apply(t) for t in texts]


@dataclass(frozen=True)
class ImageRef:
    """Image reference; the current text-only backend only rejects it explicitly."""

    url: str


EmbeddingInput = str | ImageRef
"""Mixed embedding input: text is embedded normally, image references are rejected outright."""


@dataclass(frozen=True)
class EmbeddingResult:
    """Batch embedding result; vectors and usage on the same record."""

    vectors: list[list[float]]
    usage: dict[str, int]


def _usage_of(payload: dict) -> dict[str, int]:
    raw = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {"prompt_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": int(raw.get("prompt_tokens", 0)),
        "total_tokens": int(raw.get("total_tokens", 0)),
    }


@dataclass(frozen=True)
class EmbeddingConfig:
    """Embedding endpoint config; local expected dim and remote transport params strictly separated."""

    base: str
    api_key: str
    model: str
    dim: int
    timeout: float = 30.0
    batch_size: int = EMBED_BATCH
    retries: int = 3
    preprocessor: Preprocessor = field(default_factory=Preprocessor)

    def validate(self) -> None:
        if not self.api_key:
            raise LlmConfigError("embedding api key is empty")
        if not self.base.strip():
            raise LlmConfigError("embedding base is empty")
        if not self.model:
            raise LlmConfigError("embedding model is empty")
        if self.dim <= 0:
            raise LlmConfigError("embedding dim must be positive")

    @classmethod
    def from_settings(cls, settings: LibrefSettings) -> EmbeddingConfig | None:
        """Return None when there is no API key (caller falls back to pure full-text); missing anything else raises."""
        if not settings.embedding_api_key:
            return None
        config = cls(
            base=settings.embedding_api_base,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            timeout=settings.request_timeout,
            batch_size=settings.embed_batch,
        )
        config.validate()
        known = embedding_dim_for(config.model)
        if known is not None and known != config.dim:
            raise LlmConfigError(
                f"embedding dim mismatch: model {config.model} wants"
                f" {known}, got {config.dim}"
            )
        return config


class EmbeddingClient:
    """Embedding-compatible interface; batch normalization and dim/count validation live in this layer."""

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
        retry_policy: RetryPolicy | None = None,
        on_retry: Callable[[RetryAttempt], None] | None = None,
        preprocessor: Preprocessor | None = None,
        resilience: Resilience | None = None,
    ) -> None:
        if not api_key:
            raise LlmConfigError("embedding api key is empty")
        if not base.strip():
            raise LlmConfigError("embedding base is empty")
        if not model:
            raise LlmConfigError("embedding model is empty")
        if dim <= 0:
            raise LlmConfigError("embedding dim must be positive")
        self._base = base.rstrip("/")
        self._key = api_key
        self._model = model
        self._dim = dim
        self._timeout = timeout
        self._batch = max(1, batch_size)
        self._policy = (
            retry_policy
            if retry_policy is not None
            else RetryPolicy(max_retries=retries)
        )
        self._on_retry = on_retry
        self._pre = preprocessor if preprocessor is not None else Preprocessor()
        self._resilience = resilience

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    async def encode(
        self, texts: list[str], *, client: httpx.AsyncClient | None = None
    ) -> list[list[float]]:
        """Batch embed; empty input returns empty without making a call."""
        result = await self.encode_with_usage(texts, client=client)
        return result.vectors

    async def encode_with_usage(
        self, texts: list[str], *, client: httpx.AsyncClient | None = None
    ) -> EmbeddingResult:
        """Batch embed and surface usage; empty input returns empty."""
        return await self.encode_inputs(texts, client=client)

    async def encode_inputs(
        self, inputs: Sequence[EmbeddingInput], *, client: httpx.AsyncClient | None = None
    ) -> EmbeddingResult:
        """Embed mixed inputs; image references are rejected outright without a call."""
        texts: list[str] = []
        for item in inputs:
            if isinstance(item, ImageRef):
                raise LlmInvalidRequestError("embedding image inputs are unsupported")
            texts.append(item)
        texts = self._pre.apply_batch(texts)
        if not texts:
            return EmbeddingResult([], {"prompt_tokens": 0, "total_tokens": 0})
        if client is not None:
            return await self._run_batches(client, texts)
        async with httpx.AsyncClient(timeout=self._timeout) as owned:
            return await self._run_batches(owned, texts)

    async def _run_batches(
        self, client: httpx.AsyncClient, texts: list[str]
    ) -> EmbeddingResult:
        vecs_all: list[list[float]] = []
        prompt = 0
        total = 0
        for i in range(0, len(texts), self._batch):
            vecs, usage = await self._encode_batch_with_usage(
                client, texts[i : i + self._batch]
            )
            vecs_all.extend(vecs)
            prompt += usage.get("prompt_tokens", 0)
            total += usage.get("total_tokens", 0)
        return EmbeddingResult(
            vecs_all, {"prompt_tokens": prompt, "total_tokens": total}
        )

    async def _encode_batch_with_usage(
        self, client: httpx.AsyncClient, texts: list[str]
    ) -> tuple[list[list[float]], dict[str, int]]:
        url = self._base + "/embeddings"
        headers = {"Authorization": f"Bearer {self._key}"}
        body = {"model": self._model, "input": texts}

        async def _once() -> tuple[list[list[float]], dict[str, int]]:
            return await with_resilience(self._resilience, _send_once)

        async def _send_once() -> tuple[list[list[float]], dict[str, int]]:
            try:
                resp = await client.post(url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                raise LlmTimeoutError() from exc
            except httpx.HTTPError as exc:
                raise LlmTransportError(f"provider transport failure: {exc}") from exc
            if resp.status_code == 429 or resp.status_code >= 500:
                raise LlmProviderError(
                    resp.status_code,
                    f"embedding failed: HTTP {resp.status_code}",
                    parse_retry_after_ms(resp.headers.get("retry-after")),
                )
            if resp.status_code >= 400:
                raise LlmProviderError(
                    resp.status_code, f"embedding rejected: HTTP {resp.status_code}"
                )
            try:
                payload = resp.json()
                items = payload["data"]
            except (ValueError, KeyError, TypeError) as exc:
                raise LlmDecodeError(f"embedding response missing data: {exc}") from exc
            if not isinstance(items, list):
                raise LlmDecodeError("embedding response missing data")
            usage = _usage_of(payload)
            ordered = sorted(items, key=lambda d: d["index"])
            seen: set[int] = set()
            vecs: list[list[float]] = []
            for item in ordered:
                idx = int(item["index"])
                if idx in seen:
                    raise LlmDecodeError(f"embedding response duplicate index '{idx}'")
                seen.add(idx)
                vec = item["embedding"]
                norm = sum(x * x for x in vec) ** 0.5
                vec = [x / norm for x in vec] if norm > 0 else vec
                if len(vec) != self._dim:
                    raise LlmDecodeError(
                        f"embedding dim mismatch: got {len(vec)}, want {self._dim}"
                    )
                vecs.append(vec)
            if len(vecs) != len(texts):
                raise LlmDecodeError(
                    f"embedding count mismatch: got {len(vecs)}, want {len(texts)}"
                )
            return vecs, usage

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


def build_embed_fn(
    settings: LibrefSettings, transport: httpx.AsyncBaseTransport | None = None
) -> EmbedFn | None:
    """Build the embed function from settings; returns None without an API key (caller falls back to pure full-text)."""
    config = EmbeddingConfig.from_settings(settings)
    if config is None:
        return None
    client = EmbeddingClient(
        base=config.base,
        api_key=config.api_key,
        model=config.model,
        dim=config.dim,
        timeout=config.timeout,
        batch_size=config.batch_size,
        preprocessor=config.preprocessor,
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
