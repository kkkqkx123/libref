"""Reciprocal rank fusion and rerank scoring."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from libref.store.retriever.candidates import _fetch_details
from libref.store.retriever.features import alignment_key
from libref.text.snippet import truncate_material


class Fusion:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def _rrf_scores(self, fts_ids: list[int], vec_ids: list[int]) -> dict[int, float]:
        from libref.config import setting_or_default

        k = int(setting_or_default(self._settings, "rrf_k"))
        w_fts = float(setting_or_default(self._settings, "rrf_w_fts"))
        w_vec = float(setting_or_default(self._settings, "rrf_w_vec"))
        rank_fts = {doc: i + 1 for i, doc in enumerate(fts_ids)}
        rank_vec = {doc: i + 1 for i, doc in enumerate(vec_ids)}
        scored: dict[int, float] = {}
        for doc in list(rank_fts) + [d for d in rank_vec if d not in rank_fts]:
            s = 0.0
            if doc in rank_fts:
                s += w_fts / (k + rank_fts[doc])
            if doc in rank_vec:
                s += w_vec / (k + rank_vec[doc])
            scored[doc] = s
        return scored

    def _rrf(self, fts_ids: list[int], vec_ids: list[int]) -> list[int]:
        scored = self._rrf_scores(fts_ids, vec_ids)
        return sorted(scored, key=lambda d: scored[d], reverse=True)

    def _status(
        self,
        degraded: bool,
        reasons: list[str],
        fts_ids: list[int],
        vec_ids: list[int],
        fused_order: list[int],
        rerank_applied: bool,
        answerability: dict[str, Any],
        cross_lingual: bool,
    ) -> dict[str, Any]:
        return {
            "degraded": bool(degraded),
            "reasons": list(dict.fromkeys(reasons)),
            "fts_count": len(fts_ids),
            "vec_count": len(vec_ids),
            "fused_order": list(fused_order),
            "rerank_applied": bool(rerank_applied),
            "cross_lingual": bool(cross_lingual),
            **answerability,
        }

    async def _apply_rerank(
        self,
        query: str,
        fused: list[int],
        *,
        window: int,
        top_n: int | None,
    ) -> tuple[list[int], dict[int, float], bool]:
        """Rerank the recall head; returns the reordered ids, the score map, and the failure flag."""
        from libref.config import setting_or_default

        if self._rerank is None:
            return fused, {}, False
        if top_n is not None:
            window = min(window, int(top_n))
        head, tail = fused[:window], fused[window:]
        if not head:
            return fused, {}, False
        radius = int(setting_or_default(self._settings, "snippet_radius"))
        cap = int(setting_or_default(self._settings, "snippet_cap"))
        details = _fetch_details(
            self._conn, head, query, snippet_radius=radius, snippet_cap=cap
        )
        head = [d for d in head if d in details]
        if not head:
            return tail, {}, False
        link_cap = int(setting_or_default(self._settings, "rerank_truncate"))
        total_cap = int(setting_or_default(self._settings, "material_limit"))
        link_cap = min(link_cap, total_cap)
        texts = [truncate_material(details[d]["content"], link_cap) for d in head]
        try:
            scored = await self._rerank(query, texts, len(head))
        except Exception:  # noqa: BLE001 -- rerank failure keeps fused order and is reported by the caller
            return fused, {}, True
        order = sorted(scored, key=lambda kv: kv[1], reverse=True)
        pos_of = {idx: rank for rank, (idx, _) in enumerate(order)}
        head_sorted = sorted(range(len(head)), key=lambda i: pos_of.get(i, len(order)))
        rerank_scores = {
            head[idx]: float(score) for idx, score in scored if 0 <= idx < len(head)
        }
        return [head[i] for i in head_sorted] + tail, rerank_scores, False


def _collapse_duplicates(conn: sqlite3.Connection, fused: list[int]) -> list[int]:
    """Drop the auxiliary full-text twin of a section part, then cap each source group at two chunks.

    Both chunk lanes index the same sections: the auxiliary lane only exists for lexical
    matching, so its copy never becomes user-facing material while the full-body twin is
    recalled. The per-group cap stays because merged sections legitimately split into parts.
    """
    if len(fused) < 2:
        return fused
    marks = ",".join("?" for _ in fused)
    rows = conn.execute(
        f"SELECT id, url, metadata FROM documents WHERE id IN ({marks})", fused
    ).fetchall()
    groups: dict[int, str] = {}
    lanes: dict[int, str] = {}
    parts: dict[int, str] = {}
    full_body_parts: set[str] = set()
    for doc_id, url, raw in rows:
        try:
            meta = json.loads(raw) if raw else {}
        except ValueError:
            meta = {}
        key = alignment_key(meta, url, int(doc_id))
        lane = str(meta.get("chunk_path") or "embedding")
        part = str(meta.get("chunk_index", 0) or 0)
        groups[int(doc_id)] = key
        lanes[int(doc_id)] = lane
        parts[int(doc_id)] = part
        if lane != "bm25":
            full_body_parts.add(f"{key}\x00{part}")
    counts: dict[str, int] = {}
    out: list[int] = []
    for doc_id in fused:
        group_key = groups.get(doc_id)
        if group_key is None:
            out.append(doc_id)
            continue
        if (
            lanes[doc_id] == "bm25"
            and f"{group_key}\x00{parts[doc_id]}" in full_body_parts
        ):
            continue
        seen = counts.get(group_key, 0)
        if seen >= 2:
            continue
        counts[group_key] = seen + 1
        out.append(doc_id)
    return out


def _cross_lingual_miss(query: str, fts_ids: list[int], vec_ids: list[int]) -> bool:
    """A CJK query whose lexical lane matched nothing while the vector lane did.

    The full-text lane matches literal terms, so a Chinese query over an English
    corpus cannot match there; reporting it as a failure to find evidence would
    be wrong. The vector lane carries such queries on its own, which the fused
    order already reflects: with no lexical ids the fusion leaves vector ranks
    untouched.
    """
    if fts_ids or not vec_ids:
        return False
    from libref.text.cjk import CJK_RUN_RE

    return bool(CJK_RUN_RE.search(query or ""))
