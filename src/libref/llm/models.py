"""Model catalog: registry of verified models; unknown models are unaffected, only entries are added."""

from __future__ import annotations

KNOWN_EMBEDDING_DIMS: dict[str, int] = {
    "BAAI/bge-m3": 1024,
}

KNOWN_CHAT_MODELS: frozenset[str] = frozenset(
    {
        "Qwen/Qwen3.5-4B",
    }
)

KNOWN_RERANK_MODELS: frozenset[str] = frozenset(
    {
        "BAAI/bge-reranker-v2-m3",
    }
)


def embedding_dim_for(model: str) -> int | None:
    """Expected dim for known embedding models; unknown models return None and trust explicit dims."""
    return KNOWN_EMBEDDING_DIMS.get(model)
