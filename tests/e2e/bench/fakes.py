"""Vector and rerank endpoints for the benchmark.

Two modes share one wiring: deterministic offline stand-ins (default, no API
key needed, byte-identical across runs) and the real models configured in the
repository environment file. The offline stand-ins are pure functions of the
text; the real mode calls the production transports unchanged, so the only
difference between runs is model quality.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
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
    """Vector store over the SQLite truth: vectors live in process, prepared up front.

    Mirrors the production embed-text convention (title and path first, body
    stripped of markdown), so a fusion comparison measures ranking rather than
    a different text assembly. Preparation is explicit because the real encoder
    batches over the network: a search over a missing vector is a harness bug,
    not a case to paper over.

    Each corpus becomes one dense matrix on first search, so scanning tens of
    thousands of chunks costs a matrix-vector product instead of a Python loop
    over every row.
    """

    def __init__(self, conn, embedder) -> None:
        self._conn = conn
        self._embedder = embedder
        self._cache: dict[int, list[float]] = {}
        self._matrices: dict[int, tuple[list[int], Any]] = {}
        self._batch = 256

    def _vector_rows(self, version_id: int | None = None) -> list[tuple[int, str]]:
        import json

        from libref.text.embedding import EMBEDDABLE_SQL_CONDITION

        sql = (
            "SELECT id, content, metadata FROM documents "
            f"WHERE {EMBEDDABLE_SQL_CONDITION}"
        )
        params: tuple = ()
        if version_id is not None:
            sql = (
                "SELECT id, content, metadata FROM documents "
                f"WHERE version_id=? AND {EMBEDDABLE_SQL_CONDITION}"
            )
            params = (int(version_id),)

        out: list[tuple[int, str]] = []
        for doc_id, content, raw in self._conn.execute(sql, params).fetchall():
            meta = json.loads(raw) if raw else {}
            out.append(
                (
                    int(doc_id),
                    build_embed_text(meta.get("title"), meta.get("path"), content),
                )
            )
        return out

    async def prepare(self) -> int:
        """Encode every vector-lane chunk missing from the cache; ids already known are skipped."""
        todo = [
            (doc_id, text)
            for doc_id, text in self._vector_rows()
            if doc_id not in self._cache
        ]
        for start in range(0, len(todo), self._batch):
            batch = todo[start : start + self._batch]
            vectors = await self._embedder([text for _, text in batch])
            if len(vectors) != len(batch):
                raise RuntimeError(
                    f"embedding count mismatch: got {len(vectors)}, want {len(batch)}"
                )
            for (doc_id, _), vector in zip(batch, vectors):
                self._cache[doc_id] = vector
        return len(todo)

    def _matrix(self, version_id: int) -> tuple[list[int], Any]:
        """Dense matrix of one corpus plus its row ids, built once per corpus."""
        import numpy as np

        hit = self._matrices.get(version_id)
        if hit is not None:
            return hit
        ids: list[int] = []
        rows: list[list[float]] = []
        for doc_id, _text in self._vector_rows(version_id):
            cached = self._cache.get(doc_id)
            if cached is None:
                raise RuntimeError(
                    f"vector cache misses doc {doc_id}; call prepare() before searching"
                )
            ids.append(doc_id)
            rows.append(cached)
        matrix = np.asarray(rows, dtype=np.float32)
        self._matrices[version_id] = (ids, matrix)
        return ids, matrix

    def search(
        self, query_vec: list[float], *, k: int, version_id: int
    ) -> list[tuple[int, float]]:
        import numpy as np

        ids, matrix = self._matrix(version_id)
        if not ids:
            return []
        scores = matrix @ np.asarray(query_vec, dtype=np.float32)
        order = np.lexsort((np.asarray(ids), -scores))[: max(0, int(k))]
        return [(ids[int(index)], float(scores[int(index)])) for index in order]


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


REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_ENV = REPO_ROOT / ".env"


def real_settings(db_path, **overrides) -> Any:
    """Settings backed by the repository environment file: real endpoints, rerank on."""
    from libref.config import LibrefSettings

    base: dict[str, Any] = {
        "db_path": Path(db_path),
        "vector_backend": "simvec",
        "rerank_enabled": True,
        "chat_enabled": False,
    }
    base.update(overrides)
    return LibrefSettings(_env_file=REPO_ENV, **base)


def real_embedder(settings):
    """Real embedding transport; a missing key is a harness error, not a silent degrade."""
    from libref.llm.embeddings import build_embed_fn

    embed = build_embed_fn(settings)
    if embed is None:
        raise RuntimeError(f"real embedding requested but no key in {REPO_ENV}")
    return embed


def real_rerank(settings):
    """Real rerank transport; requires the rerank switch on in the settings passed in."""
    from libref.llm.rerank import build_rerank_fn

    rerank = build_rerank_fn(settings)
    if rerank is None:
        raise RuntimeError("real rerank requested but rerank is disabled or unkeyed")
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
