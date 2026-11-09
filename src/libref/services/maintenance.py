"""Vector maintenance: rebuild Qdrant points from SQLite ground truth."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from libref.chunking import ChunkPath, ensure_hard_limit
from libref.services.libraries import mining_version_ids
from libref.text.embedding import build_embed_text, is_embeddable_metadata


async def sync_vectors_for_docs(conn, settings, vec_store, embed, store, counts, removed, inserted) -> None:
    import logging

    from libref.chunking import ChunkPath, ensure_hard_limit
    from libref.text.embedding import build_embed_text, is_embeddable_metadata

    logger = logging.getLogger(__name__)
    if vec_store is None or embed is None:
        return
    if not removed and not inserted:
        return
    try:
        vec_store.ensure_collection(
            conn,
            settings.embedding_model,
            settings.embedding_dim,
        )
    except Exception as exc:
        counts["vector_failed"] = counts.get("vector_failed", 0) + len(removed) + len(inserted)
        logger.warning("vector ensure collection failed: %s", exc)
        return
    try:
        vec_store.delete_docs(removed)
    except Exception as exc:
        counts["vector_failed"] = counts.get("vector_failed", 0) + len(removed) + len(inserted)
        logger.warning("vector delete failed: %s", exc)
        return
    try:
        docs = [
            d for d in store.get_documents(inserted)
            if is_embeddable_metadata(d["metadata"])
        ]
        hard = int(settings.chunk_embed_max_tokens)
        texts = []
        for d in docs:
            meta = d["metadata"] or {}
            text = build_embed_text(meta.get("title"), meta.get("path"), d["content"])
            clipped, was_clipped = ensure_hard_limit(text, ChunkPath.EMBEDDING, hard)
            if was_clipped:
                counts["chunk_truncated"] = counts.get("chunk_truncated", 0) + 1
                logger.warning("embedding text clipped to %d tokens for doc %d", hard, d["id"])
            texts.append(clipped)
        vecs = await embed(texts) if texts else []
        if len(vecs) != len(docs):
            raise RuntimeError("embedding count mismatch")
        vec_store.upsert_batch([(d["id"], d["library_id"], d["version_id"], vec) for d, vec in zip(docs, vecs)])
    except Exception as exc:
        counts["vector_failed"] = counts.get("vector_failed", 0) + len(inserted)
        logger.warning("vector upsert failed: %s", exc)


class MaintenanceService:
    """Full re-embedding of one library version into the derived vector index."""

    def __init__(self, conn, settings=None, *, vec_store=None, embed=None) -> None:
        self._conn = conn
        self._settings = settings
        self._vec = vec_store if embed is not None else None
        self._embed = embed if vec_store is not None else None

    async def reembed_library_async(
        self,
        library: str,
        version: str,
        ecosystem: str | None = None,
        *,
        settings=None,
        vec_store=None,
        embed=None,
    ) -> dict[str, int]:
        """Rebuild all vectors for a version, returning document and write counts."""
        settings = settings if settings is not None else self._settings
        vec = vec_store if vec_store is not None else self._vec
        fn = embed if embed is not None else self._embed
        if settings is None:
            raise ValueError("settings is required")
        if vec is None or fn is None:
            raise ValueError("vec_store and embed are required")
        vids = mining_version_ids(self._conn, library, version, ecosystem)
        if len(vids) != 1:
            raise ValueError("library version must resolve to a single version")
        version_id = vids[0]
        vec.ensure_collection(
            self._conn,
            settings.embedding_model,
            settings.embedding_dim,
        )
        vec.delete_version(version_id)
        rows = self._conn.execute(
            "SELECT id, library_id, version_id, content, metadata "
            "FROM documents WHERE version_id=? ORDER BY id",
            (version_id,),
        ).fetchall()
        if not rows:
            return {"docs": 0, "upserted": 0}
        embeddable = []
        for r in rows:
            try:
                meta = json.loads(r[4]) if r[4] else {}
            except ValueError:
                meta = {}
            if not is_embeddable_metadata(meta):
                continue
            embeddable.append(r)
        upserted = 0
        hard = int(settings.chunk_embed_max_tokens)
        for i in range(0, len(embeddable), settings.embed_batch):
            batch = embeddable[i:i + settings.embed_batch]
            texts = []
            for r in batch:
                try:
                    meta = json.loads(r[4]) if r[4] else {}
                except ValueError:
                    meta = {}
                text = build_embed_text(meta.get("title"), meta.get("path"), r[3])
                clipped, _ = ensure_hard_limit(text, ChunkPath.EMBEDDING, hard)
                texts.append(clipped)
            vecs = await fn(texts)
            if len(vecs) != len(batch):
                raise RuntimeError("embedding count mismatch")
            vec.upsert_batch([
                (r[0], r[1], r[2], v) for r, v in zip(batch, vecs)
            ])
            upserted += len(batch)
        return {"docs": len(rows), "upserted": upserted}

    def reembed_library(
        self,
        library: str,
        version: str,
        ecosystem: str | None = None,
        *,
        settings=None,
        vec_store=None,
        embed=None,
    ) -> dict[str, int]:
        """Synchronous entry for command-line use."""
        return asyncio.run(self.reembed_library_async(
            library, version, ecosystem,
            settings=settings, vec_store=vec_store, embed=embed,
        ))
