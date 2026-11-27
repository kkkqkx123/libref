"""Passage assembly and evidence window expansion."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from libref.mining.idiom import reuse_diversity
from libref.mining.scoring import (
    capped_alpha,
    content_idents,
    name_hit,
    support_for_content,
    support_for_entities,
)
from libref.store.retriever.candidates import _fetch_details
from libref.store.retriever.features import (
    _facet_weights,
    _pattern_supports,
    doc_feature_annotations,
)
from libref.text.snippet import DERIVED_TAG, truncate_material


class Assembly:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def _assemble(
        self,
        query: str,
        fused: list[int],
        fts_ids: list[int],
        vec_ids: list[int],
        *,
        rerank_scores: dict[int, float] | None = None,
    ) -> list[dict[str, Any]]:
        from libref.config import setting_or_default
        from libref.mining.scoring import facet_bonus_for_entities

        radius = int(setting_or_default(self._settings, "snippet_radius"))
        cap = int(setting_or_default(self._settings, "snippet_cap"))
        details = _fetch_details(
            self._conn, fused, query, snippet_radius=radius, snippet_cap=cap
        )
        fts_set, vec_set = set(fts_ids), set(vec_ids)
        raw_alpha = float(setting_or_default(self._settings, "freq_alpha"))
        facet_alpha = float(setting_or_default(self._settings, "facet_alpha"))
        rrf = self._rrf_scores(fts_ids, vec_ids)
        vids = sorted({details[d]["version_id"] for d in fused if d in details})
        wanted: set[str] = set()
        if raw_alpha > 0 or facet_alpha > 0:
            for doc in fused:
                info = details.get(doc)
                if info is None:
                    continue
                for entity in info.get("entities", []) or []:
                    if entity:
                        wanted.add(str(entity))
        patterns = _pattern_supports(self._conn, vids, wanted) if raw_alpha > 0 else {}
        facet_weights = _facet_weights(self._conn, vids) if facet_alpha > 0 else {}
        health_rows = [{"support": s} for plist in patterns.values() for _, s in plist]
        health = reuse_diversity(health_rows, [])
        alpha = capped_alpha(raw_alpha, float(health.get("reusability", 0.0)))
        facet_gate = capped_alpha(facet_alpha, float(health.get("reusability", 0.0)))
        scored: list[tuple[float, int]] = []
        for doc in fused:
            info = details.get(doc)
            if info is None:
                continue
            if rerank_scores:
                base = rerank_scores.get(doc, rrf.get(doc, 0.0))
            else:
                base = rrf.get(doc, 0.0)
            plist = patterns.get(info["version_id"], [])
            have_entities = list(info.get("entities", []) or [])
            if have_entities and alpha > 0:
                bonus = support_for_entities(have_entities, plist)
            else:
                bonus = (
                    support_for_content(info["content"], plist) if alpha > 0 else 0.0
                )
            facet_bonus = 0.0
            fweights = facet_weights.get(info["version_id"], [])
            if fweights and facet_gate > 0:
                if have_entities:
                    facet_bonus = facet_bonus_for_entities(have_entities, fweights)
                else:
                    content = info["content"] or ""
                    full, toks = content_idents(content)
                    for callee, _weight in fweights:
                        if name_hit(callee, full, toks):
                            facet_bonus = 1.0
                            break
            final = base * (1.0 + alpha * bonus + facet_gate * facet_bonus)
            scored.append((final, doc))
        scored.sort(key=lambda kv: kv[0], reverse=True)
        out: list[dict[str, Any]] = []
        seen_excerpts: set[tuple[str, str]] = set()
        for final, doc in scored:
            info = details[doc]
            excerpt = (str(info["url"] or ""), str(info["snippet"] or ""))
            if excerpt in seen_excerpts:
                continue
            seen_excerpts.add(excerpt)
            sources = []
            if doc in fts_set:
                sources.append("fts")
            if doc in vec_set:
                sources.append("vec")
            out.append(
                {
                    "id": info["id"],
                    "url": info["url"],
                    "score": final,
                    "snippet": info["snippet"],
                    "title": info["title"],
                    "path": info["path"],
                    "library": info["library"],
                    "version": info["version"],
                    "version_id": int(info["version_id"]),
                    "sources": sources,
                    "chunk_path": info.get("chunk_path", "embedding"),
                    "source_group_id": info.get("source_group_id", ""),
                    "chunk_id": info.get("chunk_id", ""),
                    "page_id": info.get("page_id"),
                    "sort_order": info.get("sort_order"),
                    "span_kind": info.get("span_kind", "document_range"),
                    "merged_from": list(info.get("merged_from", []) or []),
                    "overlap_from": info.get("overlap_from"),
                    "overlap_text": str(info.get("overlap_text", "") or ""),
                    "chunk_index": int(info.get("chunk_index", 0) or 0),
                    "total_chunks": max(1, int(info.get("total_chunks", 1) or 1)),
                    "keywords": list(info.get("keywords", []) or []),
                    "content_type": info.get("content_type", "text/markdown"),
                    "entities": list(info.get("entities", []) or []),
                }
            )
        return out

    def _postprocess(
        self,
        query: str,
        fused: list[int],
        fts_ids: list[int],
        vec_ids: list[int],
        *,
        expand_window: bool,
        window_radius: int,
        rerank_scores: dict[int, float] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Post-processing chain: assembly runs in order; on window-expansion failure the assembled order is kept."""
        results = self._assemble(
            query,
            fused,
            fts_ids,
            vec_ids,
            rerank_scores=rerank_scores,
        )
        results, ok = self._expand_windows(
            results, enabled=expand_window, radius=window_radius
        )
        return self._annotate_results(results), ok

    def _annotate_results(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach derived facts to top results after ranking; scores stay unchanged."""
        from libref.config import setting_or_default

        top_n = int(setting_or_default(self._settings, "doc_feature_annotation_top_n"))
        if top_n <= 0 or not results:
            return results
        if not bool(setting_or_default(self._settings, "doc_feature_enabled")):
            return results
        winners = results[:top_n]
        notes = doc_feature_annotations(
            self._conn,
            sorted({int(item["version_id"]) for item in winners}),
            [int(item["id"]) for item in winners],
        )
        for item in winners:
            lines = notes.get(int(item["id"]), [])
            if not lines:
                continue
            item["annotations"] = lines
            item["raw_snippet"] = str(item["snippet"])
            suffix = DERIVED_TAG + " " + "; ".join(lines)
            item["snippet"] = f"{item['raw_snippet']}\n\n{suffix}"
        return results

    def _expand_windows(
        self, results: list[dict[str, Any]], *, enabled: bool, radius: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Window-expansion post-processing: prepend/append neighboring chunks by the configured radius; on failure keep the original order."""
        from libref.config import setting_or_default

        if not enabled or not results:
            return results, True
        radius = max(0, int(radius))
        if radius <= 0:
            return results, True
        preview_cap = int(setting_or_default(self._settings, "window_preview_cap"))
        try:
            locs = {
                int(r[0]): (int(r[1]), int(r[2]), str(r[3] or "embedding"))
                for r in self._conn.execute(
                    f"SELECT d.id, d.page_id, d.sort_order,"
                    f" COALESCE(json_extract(d.metadata, '$.chunk_path'), 'embedding')"
                    f" FROM documents d WHERE d.id IN ({','.join('?' for _ in results)})",
                    [r["id"] for r in results],
                ).fetchall()
            }
            by_page: dict[int, list[int]] = {}
            for doc_id, (page_id, _order, _lane) in locs.items():
                by_page.setdefault(page_id, []).append(doc_id)
            windows: dict[int, list[dict[str, Any]]] = {}
            for page_id, doc_ids in by_page.items():
                orders = sorted(locs[d][1] for d in doc_ids)
                lanes = {locs[d][2] for d in doc_ids}
                lo, hi = orders[0] - radius, orders[-1] + radius
                marks = ",".join("?" for _ in lanes)
                rows = self._conn.execute(
                    "SELECT id, url, content, metadata, sort_order FROM documents "
                    "WHERE page_id=? AND sort_order BETWEEN ? AND ? "
                    f"AND COALESCE(json_extract(metadata, '$.chunk_path'), 'embedding') IN ({marks}) "
                    "ORDER BY sort_order",
                    (page_id, lo, hi, *sorted(lanes)),
                ).fetchall()
                import json as _json

                by_order: dict[int, list[dict[str, Any]]] = {}
                for wid, wurl, wcontent, wmeta, worder in rows:
                    try:
                        parsed = _json.loads(wmeta) if wmeta else {}
                    except ValueError:
                        parsed = {}
                    by_order.setdefault(int(worder), []).append(
                        {
                            "id": int(wid),
                            "url": wurl,
                            "content": wcontent,
                            "metadata": parsed,
                            "sort_order": int(worder),
                        }
                    )
                for doc_id in doc_ids:
                    order = locs[doc_id][1]
                    lane = locs[doc_id][2]
                    picked = [
                        w
                        for o in range(order - radius, order + radius + 1)
                        for w in by_order.get(o, [])
                        if str(
                            (w["metadata"] or {}).get("chunk_path", "embedding")
                            or "embedding"
                        )
                        == lane
                    ]
                    windows[doc_id] = picked
            expanded: list[dict[str, Any]] = []
            covered: set[int] = set()
            for item in results:
                doc_id = int(item["id"])
                loc = locs.get(doc_id)
                if loc is not None:
                    page_id, order, lane = loc
                    for other in results:
                        other_id = int(other["id"])
                        if other_id == doc_id:
                            continue
                        other_loc = locs.get(other_id)
                        if other_loc is None:
                            continue
                        if (
                            other_loc[0] == page_id
                            and other_loc[2] == lane
                            and abs(other_loc[1] - order) <= radius
                        ):
                            covered.add(doc_id)
                            break
                window = windows.get(int(item["id"]), [])
                if len(window) > 1 and int(item["id"]) not in covered:
                    item = dict(item)
                    item["window"] = [
                        {
                            "id": w["id"],
                            "sort_order": w["sort_order"],
                            "chunk_id": str(
                                (w.get("metadata") or {}).get("chunk_id", "") or ""
                            ),
                            "source_group_id": str(
                                (w.get("metadata") or {}).get("source_group_id", "")
                                or ""
                            ),
                            "content": truncate_material(w["content"], preview_cap),
                        }
                        for w in window
                    ]
                expanded.append(item)
            return expanded, True
        except Exception:  # noqa: BLE001 -- expansion failure keeps the original results
            return results, False
