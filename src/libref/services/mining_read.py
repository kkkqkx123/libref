"""Mining read model: call frequency, co-occurrence, patterns, trees, clusters."""

from __future__ import annotations

import json
from typing import Any

from libref.services.libraries import mining_version_ids
from libref.text.snippet import gap_tag, unit_tag, xml_escape_attr


def is_subsequence(pattern: list[str], names: list[str]) -> bool:
    """Check whether pattern appears in order inside names."""
    pos = 0
    for want in pattern:
        while pos < len(names) and names[pos] != want:
            pos += 1
        if pos == len(names):
            return False
        pos += 1
    return True


def _subsequence_positions(pattern: list[str], names: list[str]) -> list[int]:
    """Locate the first ordered occurrence of pattern inside names."""
    pos: list[int] = []
    cursor = 0
    for want in pattern:
        while cursor < len(names) and names[cursor] != want:
            cursor += 1
        if cursor >= len(names):
            return []
        pos.append(cursor)
        cursor += 1
    return pos


def _render_evidence(
    group: dict[str, Any], calls: list[dict[str, Any]], matched: list[int]
) -> str:
    """Render matched calls with neighbours and explicit gaps, body verbatim."""
    keep: set[int] = set(matched)
    if matched:
        if matched[0] - 1 >= 0:
            keep.add(matched[0] - 1)
        if matched[-1] + 1 < len(calls):
            keep.add(matched[-1] + 1)
    ordered = sorted(keep)
    parts: list[str] = []
    prev: int | None = None
    for idx in ordered:
        if prev is not None and idx - prev > 1:
            skipped = calls[prev + 1 : idx]
            lines = [c for c in skipped if c.get("line_start") is not None]
            if lines:
                span = f"{lines[0].get('line_start')}-{lines[-1].get('line_end')}"
            else:
                span = f"{prev + 1}-{idx - 1}"
            parts.append(gap_tag(len(skipped), span))
        call = calls[idx]
        args = call.get("args") or []
        body = ", ".join(str(part) for part in args)
        attrs = {
            "callee": str(call.get("callee") or ""),
            "lines": f"{call.get('line_start')}-{call.get('line_end')}",
            "confidence": str(call.get("confidence") or ""),
            "hit": "true" if idx in set(matched) else "false",
        }
        parts.append(
            f'<call callee="{xml_escape_attr(attrs["callee"])}" lines="{xml_escape_attr(attrs["lines"])}" confidence="{xml_escape_attr(attrs["confidence"])}" hit="{attrs["hit"]}">{body}</call>'
        )
        prev = idx
    inner = "\n".join(parts)
    head = {
        "file": str(group.get("caller_file") or ""),
        "caller": str(group.get("caller") or ""),
        "library": str(group.get("library") or ""),
        "version": str(group.get("version") or ""),
    }
    return unit_tag(head, inner)


class MiningReadService:
    """Read-only queries over call_instances, seq_patterns, and derived tables."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def api_usage_counts(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        base_filter = "(rs.status IS NULL OR rs.status != 'conflict')"
        if include_ambiguous:
            conf = f"AND {base_filter}"
        else:
            conf = f"AND {base_filter} AND (call_instances.confidence != 'AMBIGUOUS' OR rs.status='approved')"
        sql = (
            "SELECT callee_name, count(*), "
            "count(DISTINCT caller_file || char(0) || COALESCE(caller, '')) "
            "FROM call_instances LEFT JOIN review_status rs "
            "ON rs.entity_type='call_instance' AND rs.entity_id=call_instances.id "
            f"WHERE version_id IN ({placeholders}) {conf} "
            "GROUP BY callee_name ORDER BY count(*) DESC, callee_name"
        )
        params: list[Any] = list(vids)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            {"api": r[0], "calls": r[1], "scopes": r[2]}
            for r in self._conn.execute(sql, params).fetchall()
        ]

    def arg_shapes(
        self,
        api: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 10,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        base_filter = "(rs.status IS NULL OR rs.status != 'conflict')"
        if include_ambiguous:
            conf = f"AND {base_filter}"
        else:
            conf = f"AND {base_filter} AND (call_instances.confidence != 'AMBIGUOUS' OR rs.status='approved')"
        rows = self._conn.execute(
            "SELECT args, count(*) FROM call_instances LEFT JOIN review_status rs "
            "ON rs.entity_type='call_instance' AND rs.entity_id=call_instances.id "
            f"WHERE version_id IN ({placeholders}) AND callee_name = ? {conf} "
            "GROUP BY args ORDER BY count(*) DESC LIMIT ?",
            (*vids, api, limit),
        ).fetchall()
        out = []
        for raw, count in rows:
            try:
                parsed = json.loads(raw) if raw else []
            except ValueError:
                parsed = [raw]
            out.append({"args": parsed, "count": count})
        return out

    def related_apis(
        self,
        api: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 10,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        no_conflict_c = "(rc.status IS NULL OR rc.status != 'conflict')"
        no_conflict_t = "(rt.status IS NULL OR rt.status != 'conflict')"
        if include_ambiguous:
            conf = f"AND {no_conflict_c}"
            tconf = f"AND {no_conflict_t}"
        else:
            conf = f"AND {no_conflict_c} AND (c.confidence != 'AMBIGUOUS' OR rc.status='approved')"
            tconf = f"AND {no_conflict_t} AND (call_instances.confidence != 'AMBIGUOUS' OR rt.status='approved')"
        rows = self._conn.execute(
            "WITH target(file, caller_key) AS ("
            " SELECT DISTINCT caller_file, COALESCE(caller, '')"
            " FROM call_instances LEFT JOIN review_status rt "
            "ON rt.entity_type='call_instance' AND rt.entity_id=call_instances.id"
            f" WHERE version_id IN ({placeholders}) AND callee_name = ? {tconf})"
            " SELECT c.callee_name,"
            " count(DISTINCT c.caller_file || char(0) || COALESCE(c.caller, '')),"
            " count(*) FROM call_instances c LEFT JOIN review_status rc "
            "ON rc.entity_type='call_instance' AND rc.entity_id=c.id"
            " JOIN target t ON c.caller_file = t.file"
            " AND COALESCE(c.caller, '') = t.caller_key"
            f" WHERE c.version_id IN ({placeholders}) AND c.callee_name != ? {conf}"
            " GROUP BY c.callee_name"
            " ORDER BY 2 DESC, 3 DESC, c.callee_name LIMIT ?",
            (*vids, api, *vids, api, limit),
        ).fetchall()
        return [{"api": r[0], "scopes": r[1], "calls": r[2]} for r in rows]

    def list_patterns(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        containing: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        from libref.mining.scoring import pattern_contains_all

        vids = mining_version_ids(self._conn, library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        rows = self._conn.execute(
            "SELECT pattern, support FROM seq_patterns"
            f" WHERE version_id IN ({placeholders})"
            " ORDER BY support DESC, pattern",
            tuple(vids),
        ).fetchall()
        out = []
        for raw, support in rows:
            try:
                names = json.loads(raw)
            except ValueError:
                continue
            if containing and not pattern_contains_all(names, containing):
                continue
            out.append({"pattern": names, "support": support})
            if len(out) >= limit:
                break
        return out

    def related_common(
        self,
        apis: list[str],
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 10,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        if not apis:
            return []
        wanted = sorted({a for a in apis if a})
        if not wanted:
            return []
        vids = mining_version_ids(self._conn, library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        base_filter = "(rs.status IS NULL OR rs.status != 'conflict')"
        if include_ambiguous:
            conf = f"AND {base_filter}"
        else:
            conf = f"AND {base_filter} AND (call_instances.confidence != 'AMBIGUOUS' OR rs.status='approved')"
        rows = self._conn.execute(
            "SELECT caller_file, COALESCE(caller, ''), callee_name"
            " FROM call_instances LEFT JOIN review_status rs"
            " ON rs.entity_type='call_instance' AND rs.entity_id=call_instances.id"
            f" WHERE version_id IN ({placeholders}) {conf}",
            tuple(vids),
        ).fetchall()
        scopes: dict[tuple[str, str], set[str]] = {}
        for path, caller, callee in rows:
            scopes.setdefault((str(path), str(caller)), set()).add(str(callee))
        wanted_set = set(wanted)
        hit_keys = {k for k, have in scopes.items() if wanted_set <= have}
        if not hit_keys:
            return []
        counter: dict[str, dict[str, int]] = {}
        for key in hit_keys:
            for name in scopes[key]:
                if name in wanted_set:
                    continue
                slot = counter.setdefault(name, {"scopes": 0, "calls": 0})
                slot["scopes"] += 1
        rows = self._conn.execute(
            "SELECT callee_name, count(*) FROM call_instances"
            " LEFT JOIN review_status rs ON rs.entity_type='call_instance'"
            " AND rs.entity_id=call_instances.id"
            f" WHERE version_id IN ({placeholders}) {conf}"
            " GROUP BY callee_name",
            tuple(vids),
        ).fetchall()
        totals = {str(r[0]): int(r[1]) for r in rows}
        ranked = sorted(
            counter.items(),
            key=lambda kv: (-kv[1]["scopes"], -totals.get(kv[0], 0), kv[0]),
        )
        return [
            {"api": name, "scopes": info["scopes"], "calls": totals.get(name, 0)}
            for name, info in ranked[:limit]
        ]

    def pattern_evidence(
        self,
        pattern: list[str],
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 5,
        include_ambiguous: bool = False,
        per_file_limit: int | None = None,
        dedup: bool | None = None,
        with_snippet: bool = True,
    ) -> list[dict[str, Any]]:
        if not pattern:
            return []
        vids = mining_version_ids(self._conn, library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        base_filter = "(rs.status IS NULL OR rs.status != 'conflict')"
        if include_ambiguous:
            conf = f"AND {base_filter}"
        else:
            conf = f"AND {base_filter} AND (ci.confidence != 'AMBIGUOUS' OR rs.status='approved')"
        rows = self._conn.execute(
            "SELECT ci.version_id, l.name, v.version, ci.caller_file, ci.caller,"
            " ci.callee_name, ci.args, ci.line_start, ci.line_end, ci.confidence,"
            " ci.call_order, ci.start_byte, ci.id"
            " FROM call_instances ci LEFT JOIN review_status rs "
            "ON rs.entity_type='call_instance' AND rs.entity_id=ci.id"
            " JOIN versions v ON v.id = ci.version_id"
            " JOIN libraries l ON l.id = v.library_id"
            f" WHERE ci.version_id IN ({placeholders}) {conf}"
            " ORDER BY ci.version_id, ci.caller_file, ci.line_start,"
            " ci.start_byte, ci.call_order, ci.id",
            tuple(vids),
        ).fetchall()
        groups: dict[tuple, dict[str, Any]] = {}
        order: list[tuple] = []
        for vid, lib, ver, path, caller, callee, args, ls, le, cf, _o, _s, _i in rows:
            key = (vid, path, caller or "")
            slot = groups.get(key)
            if slot is None:
                slot = {
                    "library": lib,
                    "version": ver,
                    "caller_file": path,
                    "caller": caller,
                    "calls": [],
                }
                groups[key] = slot
                order.append(key)
            try:
                parsed = json.loads(args) if args else []
            except ValueError:
                parsed = [args]
            slot["calls"].append(
                {
                    "callee": callee,
                    "args": parsed,
                    "line_start": ls,
                    "line_end": le,
                    "confidence": cf,
                }
            )
        out = []
        for key in order:
            calls = groups[key]["calls"]
            names = [c["callee"] for c in calls]
            matched = _subsequence_positions(pattern, names)
            if not matched:
                continue
            groups[key]["matched"] = matched
            groups[key]["rendered"] = _render_evidence(groups[key], calls, matched)
            out.append(groups[key])
        from libref.config import setting_or_default
        from libref.mining.idiom import deduplicate_evidence, synthesize_snippet

        file_cap = per_file_limit
        if file_cap is None:
            try:
                file_cap = int(setting_or_default(None, "evidence_per_file_limit"))
            except Exception:  # noqa: BLE001 -- defaults keep evidence readable without settings
                file_cap = 1
        do_dedup = dedup
        if do_dedup is None:
            try:
                do_dedup = bool(setting_or_default(None, "evidence_dedup"))
            except Exception:  # noqa: BLE001 -- defaults keep evidence readable without settings
                do_dedup = True
        out = deduplicate_evidence(
            out, per_file_limit=max(1, int(file_cap)), cross_file_dedup=bool(do_dedup)
        )
        out = out[: max(1, int(limit))]
        if with_snippet:
            for group in out:
                group["snippet"] = synthesize_snippet(
                    pattern, group.get("calls") or [], group.get("matched") or []
                )
        return out

    def mining_health(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
    ) -> dict[str, Any]:
        from libref.mining.idiom import reuse_diversity

        patterns = self.list_patterns(
            library=library, version=version, ecosystem=ecosystem, limit=100000
        )
        clusters = self.api_clusters(
            library=library, version=version, ecosystem=ecosystem, limit=100000
        )
        return reuse_diversity(patterns, clusters)

    def list_labels(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        containing: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        try:
            rows = self._conn.execute(
                "SELECT pattern, pattern_hash, model, prompt_version, name,"
                " summary, category_path, workflow, related_apis"
                " FROM pattern_labels"
                f" WHERE version_id IN ({placeholders})"
                " ORDER BY pattern_hash, model, prompt_version",
                tuple(vids),
            ).fetchall()
        except Exception:  # noqa: BLE001 -- labels are optional before migration
            return []
        out = []
        for raw, phash, model, pver, name, summary, cat, workflow, related in rows:
            try:
                names = json.loads(raw) if raw else []
            except ValueError:
                continue
            if containing and not all(n in names for n in containing):
                continue
            try:
                cat_path = json.loads(cat) if cat else []
            except ValueError:
                cat_path = []
            try:
                rel = json.loads(related) if related else []
            except ValueError:
                rel = []
            out.append(
                {
                    "pattern": names,
                    "pattern_hash": phash,
                    "model": model,
                    "prompt_version": pver,
                    "name": name,
                    "summary": summary,
                    "category_path": cat_path,
                    "workflow": workflow,
                    "related_apis": rel,
                }
            )
            if len(out) >= limit:
                break
        return out

    def feature_tree(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        prefix: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        params: list[Any] = list(vids)
        filt = ""
        if prefix:
            filt = " AND (path = ? OR path LIKE ? ESCAPE '\\')"
            params.extend(
                [
                    prefix,
                    prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    + ".%",
                ]
            )
        params.append(limit)
        rows = self._conn.execute(
            "SELECT path, parent, depth, kind, calls, scopes FROM feature_nodes"
            f" WHERE version_id IN ({placeholders}){filt}"
            " ORDER BY calls DESC, path LIMIT ?",
            params,
        ).fetchall()
        return [
            {
                "path": r[0],
                "parent": r[1],
                "depth": r[2],
                "kind": r[3],
                "calls": r[4],
                "scopes": r[5],
            }
            for r in rows
        ]

    def api_clusters(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        containing: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        rows = self._conn.execute(
            "SELECT cluster_id, member, size, representative FROM api_clusters"
            f" WHERE version_id IN ({placeholders})"
            " ORDER BY cluster_id, member",
            tuple(vids),
        ).fetchall()
        grouped: dict[int, dict[str, Any]] = {}
        order: list[int] = []
        for cid, member, size, rep in rows:
            slot = grouped.get(int(cid))
            if slot is None:
                slot = {
                    "cluster_id": int(cid),
                    "members": [],
                    "size": int(size),
                    "representative": rep,
                }
                grouped[int(cid)] = slot
                order.append(int(cid))
            slot["members"].append(member)
        out = [grouped[cid] for cid in order]
        if containing:
            out = [g for g in out if all(n in g["members"] for n in containing)]
        return out[:limit]

    def api_insight(
        self,
        view: str,
        api: str | None = None,
        apis: list[str] | None = None,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Unified API view: frequency ranking, co-occurring APIs, argument shapes."""
        if view == "common":
            wanted = apis if apis is not None else ([api] if api else [])
            if not wanted:
                raise ValueError("common view requires api or apis")
            return self.related_common(
                wanted,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 10,
            )
        if view == "usage":
            return self.api_usage_counts(
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 20,
            )
        if view == "related":
            if not api:
                raise ValueError("related view requires api")
            return self.related_apis(
                api,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 10,
            )
        if view == "args":
            if not api:
                raise ValueError("args view requires api")
            return self.arg_shapes(
                api,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 10,
            )
        raise ValueError(f"unsupported api view: {view}")

    def pattern_insight(
        self,
        view: str,
        pattern: list[str] | None = None,
        containing: list[str] | None = None,
        prefix: str | None = None,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int | None = None,
        per_file_limit: int | None = None,
        dedup: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Unified pattern view: patterns, evidence, feature tree, clusters."""
        if view == "health":
            return [
                self.mining_health(
                    library=library, version=version, ecosystem=ecosystem
                )
            ]
        if view == "labels":
            return self.list_labels(
                library=library,
                version=version,
                ecosystem=ecosystem,
                containing=containing,
                limit=limit if limit is not None else 50,
            )
        if view == "snippets":
            if not pattern:
                raise ValueError("snippets view requires a non-empty pattern")
            groups = self.pattern_evidence(
                pattern,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 5,
                per_file_limit=per_file_limit,
                dedup=dedup,
                with_snippet=True,
            )
            return [
                {
                    "caller_file": g.get("caller_file"),
                    "caller": g.get("caller"),
                    "snippet": g.get("snippet"),
                    "rendered": g.get("rendered"),
                }
                for g in groups
            ]
        if view == "patterns":
            return self.list_patterns(
                library=library,
                version=version,
                ecosystem=ecosystem,
                containing=containing,
                limit=limit if limit is not None else 50,
            )
        if view == "evidence":
            if not pattern:
                raise ValueError("evidence view requires a non-empty pattern")
            return self.pattern_evidence(
                pattern,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 5,
                per_file_limit=per_file_limit,
                dedup=dedup,
                with_snippet=True,
            )
        if view == "tree":
            return self.feature_tree(
                library=library,
                version=version,
                ecosystem=ecosystem,
                prefix=prefix,
                limit=limit if limit is not None else 100,
            )
        if view == "clusters":
            return self.api_clusters(
                library=library,
                version=version,
                ecosystem=ecosystem,
                containing=containing,
                limit=limit if limit is not None else 50,
            )
        raise ValueError(f"unsupported pattern view: {view}")
