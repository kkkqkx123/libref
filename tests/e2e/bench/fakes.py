"""Deterministic offline stand-ins for the vector and rerank services.

The benchmark must run without an embedding API or Qdrant, yet still exercise
the real fusion, dedup and rerank wiring. Both stand-ins are pure functions of
the text, so two runs over the same corpus produce identical vectors, scores
and rankings.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from libref.text.cjk import CJK_RUN_RE, expand_cjk_run
from libref.text.embedding import build_embed_text

_LATIN_RE = re.compile(r"[a-z0-9_]+")

BENCH_DIM = 256


def _terms(text: str) -> list[str]:
    """Term stream matching production tokenization: lowercase latin runs plus CJK expansions."""
    lowered = (text or "").lower()
    terms = _LATIN_RE.findall(lowered)
    for run in CJK_RUN_RE.findall(lowered):
        terms.extend(expand_cjk_run(run))
    return terms


def _bucket(term: str, dim: int) -> int:
    digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dim


class BagOfTermsEmbedder:
    """Lexical pseudo-embedding: hashed term frequencies, L2 normalized."""

    def __init__(self, dim: int = BENCH_DIM) -> None:
        self.dim = int(dim)

    def vector_for(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for term in _terms(text):
            vector[_bucket(term, self.dim)] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 0:
            return vector
        return [value / norm for value in vector]

    async def __call__(self, texts: list[str]) -> list[list[float]]:
        return [self.vector_for(text) for text in texts]


class SqliteVecStore:
    """Vector store over the SQLite truth: encodes stored chunks on demand.

    Mirrors the production embed-text convention (title and path first, body
    stripped of markdown), so a fusion comparison measures ranking rather than
    a different text assembly.
    """

    def __init__(self, conn, embedder: BagOfTermsEmbedder) -> None:
        self._conn = conn
        self._embedder = embedder
        self._cache: dict[int, list[float]] = {}

    def _vectors(self, version_id: int) -> list[tuple[int, list[float]]]:
        rows = self._conn.execute(
            "SELECT d.id, d.content, d.metadata FROM documents d "
            "WHERE d.version_id=? AND "
            "COALESCE(json_extract(d.metadata, '$.chunk_path'), 'embedding') != 'bm25'",
            (int(version_id),),
        ).fetchall()
        import json

        out: list[tuple[int, list[float]]] = []
        for doc_id, content, raw in rows:
            cached = self._cache.get(int(doc_id))
            if cached is None:
                meta = json.loads(raw) if raw else {}
                text = build_embed_text(meta.get("title"), meta.get("path"), content)
                cached = self._embedder.vector_for(text)
                self._cache[int(doc_id)] = cached
            out.append((int(doc_id), cached))
        return out

    def search(
        self, query_vec: list[float], *, k: int, version_id: int
    ) -> list[tuple[int, float]]:
        scored: list[tuple[float, int]] = []
        for doc_id, vector in self._vectors(version_id):
            score = sum(a * b for a, b in zip(query_vec, vector))
            scored.append((score, doc_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [(doc_id, score) for score, doc_id in scored[: max(0, int(k))]]

    def count(self, version_id: int) -> int:
        return len(self._vectors(version_id))

    def list_ids(self, version_id: int, *, batch: int = 512) -> list[int]:
        return [doc_id for doc_id, _ in self._vectors(version_id)]


def lexical_rerank():
    """Deterministic cross-encoder stand-in: query-term coverage of the document."""

    async def rerank(
        query: str, documents: list[str], top_n: int | None
    ) -> list[tuple[int, float]]:
        wanted = set(_terms(query))
        scored: list[tuple[int, float]] = []
        for index, document in enumerate(documents):
            have = set(_terms(document))
            overlap = len(wanted & have)
            scored.append((index, overlap / len(wanted) if wanted else 0.0))
        return scored

    return rerank


def failing_rerank():
    """Rerank stand-in that always fails, for degradation-path measurements."""

    async def rerank(query: str, documents: list[str], top_n: int | None):
        raise RuntimeError("benchmark rerank failure")

    return rerank


def bench_settings(db_path, **overrides) -> Any:
    """Hermetic settings for offline benchmarking: no .env, no LIBREF_* leak."""
    import os
    from pathlib import Path

    from libref.config import LibrefSettings

    saved = {
        name: os.environ.pop(name)
        for name in [name for name in os.environ if name.startswith("LIBREF_")]
    }
    base: dict[str, Any] = {
        "db_path": Path(db_path),
        "vector_backend": "simvec",
        "rerank_enabled": False,
        "chat_enabled": False,
    }
    base.update(overrides)
    try:
        return LibrefSettings(_env_file=None, **base)
    finally:
        os.environ.update(saved)
