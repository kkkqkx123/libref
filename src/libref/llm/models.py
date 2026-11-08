"""模型目录，已验证模型的数据登记；未知模型不受影响，只加条目。"""

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
    """已知编码模型的期望维度，未知模型返回空并信任显式维度。"""
    return KNOWN_EMBEDDING_DIMS.get(model)
