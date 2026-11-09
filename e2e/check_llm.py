"""Live-call verification: call embedding and rerank through the real transport layer and validate response shapes.

Secrets are injected via the repo-root .env, never hardcoded in the script; actual calls are skipped without a key.
Usage: uv run python e2e/check_llm.py
"""

from __future__ import annotations

import asyncio
import math

from libref.store.chat import build_chat_fn
from libref.store.embeddings import build_embed_fn
from libref.store.rerank import build_rerank_fn

from libref.config import LibrefSettings


def _check_vectors(vecs: list[list[float]], dim: int) -> None:
    assert len(vecs) == 2, f"count mismatch: {len(vecs)}"
    for i, vec in enumerate(vecs):
        assert len(vec) == dim, f"vec {i} dim {len(vec)} != {dim}"
        norm = math.sqrt(sum(x * x for x in vec))
        assert abs(norm - 1.0) < 1e-3, f"vec {i} not normalized: norm={norm}"
    same = all(a == b for a, b in zip(vecs[0], vecs[1]))
    assert not same, "distinct inputs produced identical vectors"


def _check_rerank(out: list[tuple[int, float]], n_docs: int) -> None:
    assert len(out) == n_docs, f"rerank count {len(out)} != {n_docs}"
    assert sorted(i for i, _ in out) == list(range(n_docs)), "rerank indices incomplete"
    scores = [s for _, s in out]
    assert scores == sorted(scores, reverse=True), "rerank not sorted desc"


async def _main() -> int:
    settings = LibrefSettings()
    if not settings.embedding_api_key:
        print("SKIP: no embedding api key in .env")
        return 0
    # Rerank and chat are off by default; enable them in memory only for this check, never touching .env.
    settings.rerank_enabled = True
    settings.chat_enabled = True
    settings.chat_timeout = 120.0

    embed = build_embed_fn(settings)
    assert embed is not None, "embed builder returned None despite key present"
    texts = ["Hello, world!", "libref 使用 uv 做依赖管理"]
    vecs = await embed(texts)
    _check_vectors(vecs, settings.embedding_dim)
    print(
        f"embed ok: model={settings.embedding_model} "
        f"count={len(vecs)} dim={len(vecs[0])}"
    )

    rerank = build_rerank_fn(settings)
    assert rerank is not None, "rerank builder returned None despite key present"
    docs = ["apple", "banana", "fruit", "vegetable"]
    out = await rerank("Apple", docs, len(docs))
    _check_rerank(out, len(docs))
    top = ", ".join(f"{docs[i]}={s:.4f}" for i, s in out)
    print(f"rerank ok: model={settings.rerank_model} order: {top}")

    chat = build_chat_fn(settings)
    assert chat is not None, "chat builder returned None despite key present"
    reply = await chat([{"role": "user", "content": "用一句话介绍 uv"}])
    assert reply.strip(), "empty chat reply"
    print(f"chat ok: model={settings.chat_model} reply={reply.strip()[:60]}")
    return 0


def main() -> int:
    try:
        return asyncio.run(_main())
    except Exception as exc:  # noqa: BLE001 — the script itself reports and exits non-zero
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
