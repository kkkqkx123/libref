"""Vector maintenance: incremental sync, per-version reconciliation, full re-embedding.

SQLite is the source of truth; the vector index is derived and repaired by
set difference, never by rolling back truth writes. Vector failures are only
counted, never fatal to ingestion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

from libref.chunking import ChunkPath, ensure_hard_limit
from libref.services.registry.libraries import mining_version_ids
from libref.text.embedding import (
    EMBEDDABLE_SQL_CONDITION,
    build_embed_text,
    is_embeddable_metadata,
)

logger = logging.getLogger(__name__)

_COLLECTION_MUTEX = threading.Lock()
"""In-process guard for the collection-governance critical section.

Recreation now only happens on dimension changes, which are operator-driven;
per-version writers stay serialized by their own version locks.
"""


def ensure_collection_guarded(conn, vec_store, model: str, dim: int) -> None:
    """Run collection governance under the process-wide mutex; short sync section, no awaits inside."""
    with _COLLECTION_MUTEX:
        vec_store.ensure_collection(conn, model, dim)


def embeddable_doc_ids(conn, version_id: int) -> list[int]:
    """Ids of a version that belong in vectors; bm25-only chunks serve FTS and are excluded."""
    rows = conn.execute(
        f"SELECT id FROM documents WHERE version_id=? AND {EMBEDDABLE_SQL_CONDITION} "
        "ORDER BY id",
        (version_id,),
    ).fetchall()
    return [int(r[0]) for r in rows]


def sqlite_embeddable_count(conn, version_id: int) -> int:
    """Reconciliation baseline on the truth side; mirrors the embeddable filter above."""
    row = conn.execute(
        f"SELECT count(*) FROM documents WHERE version_id=? AND {EMBEDDABLE_SQL_CONDITION}",
        (version_id,),
    ).fetchone()
    return int(row[0])


async def _embed_rows(
    rows, settings, embed, tenant_id: str | None = None
) -> list[tuple]:
    """Encode truth rows into vector points in configured batches."""
    hard = int(settings.chunk_embed_max_tokens)
    batch = max(1, int(settings.embed_batch))
    points: list[tuple] = []
    for start in range(0, len(rows), batch):
        group = rows[start : start + batch]
        texts = []
        for r in group:
            try:
                meta = json.loads(r[4]) if len(r) > 4 and r[4] else {}
            except ValueError:
                meta = {}
            text = build_embed_text(meta.get("title"), meta.get("path"), r[3])
            clipped, _ = ensure_hard_limit(text, ChunkPath.EMBEDDING, hard)
            texts.append(clipped)
        vecs = await embed(texts) if texts else []
        if len(vecs) != len(group):
            raise RuntimeError("embedding count mismatch")
        for r, v in zip(group, vecs):
            tenant = str(r[5]) if len(r) > 5 and r[5] else (tenant_id or "default")
            points.append((r[0], r[1], r[2], v, tenant))
    return points


def _version_drifted(conn, version_id: int, model: str, dim: int) -> bool:
    from libref.store.vec_store import read_version_meta

    old_model, old_dim = read_version_meta(conn, version_id)
    if old_model is None and old_dim is None:
        return False
    return (old_model, old_dim) != (model, dim)


def version_drifted(conn, settings, version_id: int) -> bool:
    """Whether a version needs re-embedding under current settings."""
    return _version_drifted(
        conn, version_id, settings.embedding_model, int(settings.embedding_dim)
    )


def rebuild_fts(conn) -> dict[str, int]:
    """Rebuild the full-text index from the source tables, then verify."""
    conn.execute("INSERT INTO documents_fts(documents_fts) VALUES('rebuild')")
    conn.commit()
    return check_fts(conn)


def _fts_probe_token(title: str | None, content: str | None) -> str | None:
    """One distinctive index term for a document, if its text yields any."""
    import re

    words = re.findall(r"[A-Za-z0-9_]{4,}", f"{title or ''} {content or ''}")
    if words:
        return f'"{max(words, key=len)}"'
    from libref.text.query import safe_fts_query

    shaped = safe_fts_query(f"{title or ''} {content or ''}")
    if not shaped:
        return None
    return shaped.split(" OR ", 1)[0]


def check_fts(conn, *, sample: int = 20) -> dict[str, int]:
    """Check trigger presence plus spot MATCH lookups.

    Non-MATCH reads are served from the content table, so only MATCH
    queries observe the index; verify a head and tail sample instead.
    Rebuild stays idempotent for full repair.
    """
    docs = int(conn.execute("SELECT count(*) FROM documents").fetchone()[0])
    triggers = int(
        conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger'"
            " AND name IN ('documents_fts_after_insert',"
            " 'documents_fts_after_delete', 'documents_fts_after_update')"
        ).fetchone()[0]
    )
    half = max(1, max(0, int(sample)) // 2)
    rows = conn.execute(
        "SELECT id, content, title, path FROM documents ORDER BY id LIMIT ?",
        (half,),
    ).fetchall()
    rows += conn.execute(
        "SELECT id, content, title, path FROM documents ORDER BY id DESC LIMIT ?",
        (half,),
    ).fetchall()
    sampled = 0
    missing = 0
    for doc_id, content, title, path in rows:
        token = _fts_probe_token(title, content or path)
        if token is None:
            continue
        sampled += 1
        hit = conn.execute(
            "SELECT 1 FROM documents_fts WHERE rowid=? AND documents_fts MATCH ? LIMIT 1",
            (int(doc_id), token),
        ).fetchone()
        if hit is None:
            missing += 1
    return {
        "documents": docs,
        "triggers": triggers,
        "sampled": sampled,
        "missing": missing,
        "in_sync": int(triggers == 3 and missing == 0),
    }


def _record_version(conn, version_id: int, model: str, dim: int) -> None:
    from libref.store.vec_store import write_version_meta

    write_version_meta(conn, version_id, model, dim)


async def reconcile_version(
    conn, settings, vec_store, embed, version_id: int, tenant_id: str | None = None
) -> dict:
    """Repair one version by set difference; model drift rebuilds only that version.

    Returns repair statistics; raises when the vector pair is missing.
    """
    if vec_store is None or embed is None:
        raise ValueError("vec_store and embed are required")
    model = settings.embedding_model
    dim = int(settings.embedding_dim)
    ensure_collection_guarded(conn, vec_store, model, dim)
    if _version_drifted(conn, version_id, model, dim):
        try:
            vec_store.delete_version(version_id, tenant_id=tenant_id)
        except TypeError:
            vec_store.delete_version(version_id)
        try:
            has_tenant_col = any(
                r[1] == "tenant_id"
                for r in conn.execute("PRAGMA table_info(documents)").fetchall()
            )
        except Exception:  # noqa: BLE001
            has_tenant_col = False
        sel = (
            "SELECT id, library_id, version_id, content, metadata"
            + (", tenant_id" if has_tenant_col else "")
            + f" FROM documents WHERE version_id=? AND {EMBEDDABLE_SQL_CONDITION} ORDER BY id"
        )
        rows = conn.execute(sel, (version_id,)).fetchall()
        if tenant_id not in (None, "*") and has_tenant_col:
            rows = [r for r in rows if str(r[5] or "default") == str(tenant_id)]
        points = await _embed_rows(rows, settings, embed, tenant_id)
        if points:
            vec_store.upsert_batch(points)
        _record_version(conn, version_id, model, dim)
        return {
            "sqlite_embeddable": len(rows),
            "vector_before": 0,
            "vector_after": len(points),
            "missing": len(points),
            "orphans": 0,
            "upserted": len(points),
            "rebuilt": 1,
        }
    try:
        _has_t = any(
            r[1] == "tenant_id"
            for r in conn.execute("PRAGMA table_info(documents)").fetchall()
        )
    except Exception:  # noqa: BLE001
        _has_t = False
    if tenant_id not in (None, "*") and _has_t:
        _rows = conn.execute(
            f"SELECT id FROM documents WHERE version_id=? AND {EMBEDDABLE_SQL_CONDITION} AND tenant_id=? ORDER BY id",
            (version_id, str(tenant_id)),
        ).fetchall()
        want = {int(r[0]) for r in _rows}
    else:
        want = set(embeddable_doc_ids(conn, version_id))
    try:
        have = {int(i) for i in vec_store.list_ids(version_id, tenant_id=tenant_id)}
    except TypeError:
        have = {int(i) for i in vec_store.list_ids(version_id)}
    orphan = sorted(have - want)
    missing = sorted(want - have)
    if orphan:
        vec_store.delete_docs(orphan)
    upserted = 0
    if missing:
        marks = ",".join("?" for _ in missing)
        try:
            _hc = any(
                r[1] == "tenant_id"
                for r in conn.execute("PRAGMA table_info(documents)").fetchall()
            )
        except Exception:  # noqa: BLE001
            _hc = False
        _sel2 = (
            "SELECT id, library_id, version_id, content, metadata"
            + (", tenant_id" if _hc else "")
            + f" FROM documents WHERE id IN ({marks})"
        )
        rows = conn.execute(
            _sel2,
            missing,
        ).fetchall()
        by_id = {int(r[0]): r for r in rows}
        ordered = [by_id[i] for i in missing if i in by_id]
        points = await _embed_rows(ordered, settings, embed, tenant_id)
        if points:
            vec_store.upsert_batch(points)
            upserted = len(points)
    _record_version(conn, version_id, model, dim)
    return {
        "sqlite_embeddable": len(want),
        "vector_before": len(have),
        "vector_after": len(have - set(orphan)) + upserted,
        "missing": len(missing),
        "orphans": len(orphan),
        "upserted": upserted,
        "rebuilt": 0,
    }


def _row_embeddable(raw_meta) -> bool:
    try:
        meta = json.loads(raw_meta) if raw_meta else {}
    except ValueError:
        meta = {}
    return is_embeddable_metadata(meta)


async def sync_vectors_for_docs(
    conn,
    settings,
    vec_store,
    embed,
    store,
    counts,
    removed,
    inserted,
    tenant_id: str | None = None,
    *,
    version_id: int | None = None,
    repair: bool = True,
    verify: bool = True,
) -> None:
    """Sync the scrape delta, then verify the version by set and repair drift.

    Crash leftovers surface as set mismatches and are repaired here or at the
    next run of the same version; vector failures are recorded, never fatal.
    With verify disabled only the delta is pushed plus a cheap count read-back;
    set verification and drift rebuild are deferred to the closing call.
    """
    if vec_store is None or embed is None:
        return
    if version_id is None and not removed and not inserted:
        return
    model = settings.embedding_model
    dim = int(settings.embedding_dim)
    try:
        ensure_collection_guarded(conn, vec_store, model, dim)
    except Exception as exc:  # noqa: BLE001 — vector failures are recorded, not fatal
        counts["vector_failed"] = (
            counts.get("vector_failed", 0) + len(removed) + len(inserted)
        )
        logger.warning("vector ensure collection failed: %s", exc)
        return
    if (
        verify
        and version_id is not None
        and _version_drifted(conn, version_id, model, dim)
    ):
        try:
            stats = await reconcile_version(
                conn, settings, vec_store, embed, version_id, tenant_id
            )
        except Exception as exc:  # noqa: BLE001 — vector failures are recorded, not fatal
            counts["vector_failed"] = (
                counts.get("vector_failed", 0) + len(removed) + len(inserted)
            )
            logger.warning("vector drift rebuild failed: %s", exc)
            return
        counts["vector_failed"] = counts.get("vector_failed", 0)
        counts["vector_reconciled"] = (
            counts.get("vector_reconciled", 0) + stats["upserted"] + stats["orphans"]
        )
        return
    if not removed and not inserted:
        if version_id is not None and verify:
            await _verify_version(
                conn,
                settings,
                vec_store,
                embed,
                counts,
                version_id,
                repair=repair,
                tenant_id=tenant_id,
            )
        return
    try:
        vec_store.delete_docs(removed)
    except Exception as exc:  # noqa: BLE001 — vector failures are recorded, not fatal
        counts["vector_failed"] = (
            counts.get("vector_failed", 0) + len(removed) + len(inserted)
        )
        logger.warning("vector delete failed: %s", exc)
        return
    try:
        try:
            _ht3 = any(
                r[1] == "tenant_id"
                for r in conn.execute("PRAGMA table_info(documents)").fetchall()
            )
        except Exception:  # noqa: BLE001
            _ht3 = False
        _id_to_tenant: dict[int, str] = {}
        if _ht3 and inserted:
            try:
                _marks = ",".join("?" for _ in inserted)
                for _rid, _rt in conn.execute(
                    f"SELECT id, tenant_id FROM documents WHERE id IN ({_marks})",
                    inserted,
                ).fetchall():
                    _id_to_tenant[int(_rid)] = str(_rt or "default")
            except Exception:  # noqa: BLE001, S110
                pass
        docs = [
            d
            for d in store.get_documents(inserted)
            if is_embeddable_metadata(d["metadata"])
        ]
        if tenant_id not in (None, "*"):
            docs = [
                d
                for d in docs
                if _id_to_tenant.get(int(d["id"]), tenant_id or "default")
                == str(tenant_id)
            ]
        rows = [
            (
                d["id"],
                d["library_id"],
                d["version_id"],
                d["content"],
                json.dumps(d["metadata"] or {}, ensure_ascii=False),
                _id_to_tenant.get(int(d["id"]), tenant_id or "default"),
            )
            for d in docs
        ]
        points = await _embed_rows(rows, settings, embed, tenant_id)
        if points:
            vec_store.upsert_batch(points)
        if version_id is not None and verify:
            _record_version(conn, version_id, model, dim)
    except Exception as exc:  # noqa: BLE001 — vector failures are recorded, not fatal
        counts["vector_failed"] = counts.get("vector_failed", 0) + len(inserted)
        logger.warning("vector upsert failed: %s", exc)
        return
    if version_id is not None:
        if verify:
            await _verify_version(
                conn,
                settings,
                vec_store,
                embed,
                counts,
                version_id,
                repair=repair,
                tenant_id=tenant_id,
            )
        else:
            try:
                try:
                    counts["vector_count"] = int(
                        vec_store.count(version_id, tenant_id=tenant_id)
                    )
                except TypeError:
                    counts["vector_count"] = int(vec_store.count(version_id))
            except Exception as exc:  # noqa: BLE001 — read-back failure counts, repair is deferred
                counts["vector_failed"] = counts.get("vector_failed", 0) + 1
                logger.warning("vector count read-back failed: %s", exc)


async def _verify_version(
    conn,
    settings,
    vec_store,
    embed,
    counts,
    version_id: int,
    *,
    repair: bool = True,
    tenant_id: str | None = None,
) -> None:
    """Verify by count then id set; mismatch falls through to set-difference repair."""
    try:
        try:
            _hc4 = any(
                r[1] == "tenant_id"
                for r in conn.execute("PRAGMA table_info(documents)").fetchall()
            )
        except Exception:  # noqa: BLE001
            _hc4 = False
        if tenant_id not in (None, "*") and _hc4:
            want_count = int(
                conn.execute(
                    f"SELECT count(*) FROM documents WHERE version_id=? AND {EMBEDDABLE_SQL_CONDITION} AND tenant_id=?",
                    (version_id, str(tenant_id)),
                ).fetchone()[0]
            )
        else:
            want_count = sqlite_embeddable_count(conn, version_id)
        try:
            have_count = int(vec_store.count(version_id, tenant_id=tenant_id))
        except TypeError:
            have_count = int(vec_store.count(version_id))
    except Exception as exc:  # noqa: BLE001 — verification failure counts, repair is skipped
        counts["vector_failed"] = counts.get("vector_failed", 0) + 1
        logger.warning("vector verify failed: %s", exc)
        return
    if want_count == have_count:
        try:
            try:
                _hc5 = any(
                    r[1] == "tenant_id"
                    for r in conn.execute("PRAGMA table_info(documents)").fetchall()
                )
            except Exception:  # noqa: BLE001
                _hc5 = False
            if tenant_id not in (None, "*") and _hc5:
                want_ids = {
                    int(r[0])
                    for r in conn.execute(
                        f"SELECT id FROM documents WHERE version_id=? AND {EMBEDDABLE_SQL_CONDITION} AND tenant_id=?",
                        (version_id, str(tenant_id)),
                    ).fetchall()
                }
            else:
                want_ids = set(embeddable_doc_ids(conn, version_id))
            try:
                have_ids = {
                    int(i) for i in vec_store.list_ids(version_id, tenant_id=tenant_id)
                }
            except TypeError:
                have_ids = {int(i) for i in vec_store.list_ids(version_id)}
        except Exception as exc:  # noqa: BLE001 — verification failure counts, repair is skipped
            counts["vector_failed"] = counts.get("vector_failed", 0) + 1
            logger.warning("vector verify failed: %s", exc)
            return
        if want_ids == have_ids:
            return
    if not repair:
        logger.warning(
            "vector drift detected: want=%d have=%d (repair deferred)",
            want_count,
            have_count,
        )
        return
    try:
        stats = await reconcile_version(
            conn, settings, vec_store, embed, version_id, tenant_id
        )
    except Exception as exc:  # noqa: BLE001 — vector failures are recorded, not fatal
        counts["vector_failed"] = counts.get("vector_failed", 0) + abs(
            want_count - have_count
        )
        logger.warning("vector reconcile failed: %s", exc)
        return
    counts["vector_reconciled"] = (
        counts.get("vector_reconciled", 0) + stats["upserted"] + stats["orphans"]
    )


class MaintenanceService:
    """Full re-embedding of one library version into the derived vector index."""

    def __init__(self, conn, settings=None, *, vec_store=None, embed=None) -> None:
        from libref.store.vec_store import paired_vector

        self._conn = conn
        self._settings = settings
        self._vec, self._embed = paired_vector(vec_store, embed)

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
        ensure_collection_guarded(
            self._conn,
            vec,
            settings.embedding_model,
            settings.embedding_dim,
        )
        vec.delete_version(version_id)
        total = self._conn.execute(
            "SELECT count(*) FROM documents WHERE version_id=?", (version_id,)
        ).fetchone()
        rows = self._conn.execute(
            "SELECT id, library_id, version_id, content, metadata "
            f"FROM documents WHERE version_id=? AND {EMBEDDABLE_SQL_CONDITION}"
            " ORDER BY id",
            (version_id,),
        ).fetchall()
        if not rows:
            _record_version(
                self._conn,
                version_id,
                settings.embedding_model,
                int(settings.embedding_dim),
            )
            return {"docs": int(total[0]) if total else 0, "upserted": 0}
        embeddable = rows
        upserted = 0
        hard = int(settings.chunk_embed_max_tokens)
        for i in range(0, len(embeddable), settings.embed_batch):
            batch = embeddable[i : i + settings.embed_batch]
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
            vec.upsert_batch([(r[0], r[1], r[2], v) for r, v in zip(batch, vecs)])
            upserted += len(batch)
        _record_version(
            self._conn,
            version_id,
            settings.embedding_model,
            int(settings.embedding_dim),
        )
        return {"docs": int(total[0]) if total else 0, "upserted": upserted}

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
        return asyncio.run(
            self.reembed_library_async(
                library,
                version,
                ecosystem,
                settings=settings,
                vec_store=vec_store,
                embed=embed,
            )
        )
