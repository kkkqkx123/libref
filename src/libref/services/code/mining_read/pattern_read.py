"""Sequence pattern, evidence and signature reads."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from libref.services.code.mining_evidence import (
    canonical_url,
    choose_canonical,
    render_evidence,
    source_labels,
    subsequence_positions,
)
from libref.services.code.mining_read.common import (
    ProfileContext,
    _parse_json_list,
    _profile_context,
)
from libref.services.registry.libraries import mining_version_ids


class PatternReader:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def list_patterns(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        containing: list[str] | None = None,
        limit: int = 50,
        profile: str | None = None,
        *,
        _ctx: ProfileContext | None = None,
    ) -> list[dict[str, Any]]:
        from libref.mining.scoring import pattern_contains_all

        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return []
        pids = (_ctx or _profile_context(self._conn, vids, profile))[0]
        if not pids:
            return []
        placeholders = ",".join("?" for _ in pids)
        # Without a membership filter the database returns the top rows directly;
        # with one the cursor stops as soon as enough matches are collected.
        tail = "" if containing else " LIMIT ?"
        params: list[Any] = list(pids)
        if not containing:
            params.append(max(1, int(limit)))
        cursor = self._conn.execute(
            "SELECT pattern, support FROM seq_patterns"
            f" WHERE profile_id IN ({placeholders})"
            f" ORDER BY support DESC, pattern{tail}",
            params,
        )
        out = []
        for raw, support in cursor:
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

    def pattern_evidence(
        self,
        pattern: list[str],
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 5,
        include_ambiguous: bool = False,
        include_unconfirmed: bool = False,
        per_file_limit: int | None = None,
        dedup: bool | None = None,
        with_snippet: bool = True,
        profile: str | None = None,
    ) -> list[dict[str, Any]]:
        if not pattern:
            return []
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        _pids, sources, _profiles = _profile_context(self._conn, vids, profile)
        if not sources:
            return []
        macro_policy = next(iter(_profiles.values()), {}).get("macro_policy", "exclude")
        if macro_policy == "exclude":
            macro_filter = " AND COALESCE(ci.relation_kind,'direct') != 'macro'"
        elif macro_policy == "approved_only":
            macro_filter = (
                " AND (COALESCE(ci.relation_kind,'direct') != 'macro'"
                " OR rs.status='approved')"
            )
        else:
            macro_filter = ""
        macro_names: set[str] = set()
        if macro_policy == "exclude":
            try:
                macro_names = {
                    str(row[0])
                    for row in self._conn.execute(
                        "SELECT qualified_name FROM api_entities WHERE version_id IN ("
                        + ",".join("?" for _ in vids)
                        + ") AND kind='macro'",
                        tuple(vids),
                    ).fetchall()
                }
            except Exception:  # noqa: BLE001 -- read models may run on partial schemas
                macro_names = set()
        source_marks = ",".join("?" for _ in sources)
        source_params = list(sources)
        base_filter = "(rs.status IS NULL OR rs.status != 'conflict')"
        if include_unconfirmed:
            conf = f"AND {base_filter}"
        elif include_ambiguous:
            conf = (
                f"AND {base_filter} "
                "AND COALESCE(ci.target_status,'resolved_target')"
                " != 'version_unconfirmed'"
            )
        else:
            conf = (
                f"AND {base_filter} "
                "AND (ci.confidence != 'AMBIGUOUS' OR rs.status='approved')"
                " AND (COALESCE(ci.resolve_kind,'') NOT IN ('stdlib','builtin')"
                " OR rs.status='approved')"
                " AND COALESCE(ci.target_status,'resolved_target')"
                " NOT IN ('prefix_candidate', 'version_unconfirmed')"
            )
        conf += macro_filter
        # Scopes missing any member can never contain the pattern as a subsequence,
        # so only those scopes are loaded in the second phase.
        members = sorted(set(pattern))
        marks_w = ",".join("?" for _ in members)
        scope_rows = self._conn.execute(
            f"SELECT ci.scope"
            " FROM call_instances ci LEFT JOIN review_status rs "
            "ON rs.entity_type='call_instance' AND rs.entity_id=ci.id"
            f" WHERE ci.version_id IN ({placeholders})"
            f" AND ci.callee_name IN ({marks_w}) AND ci.in_target_api=1"
            f" AND ci.source_id IN ({source_marks}) {conf}"
            f" GROUP BY ci.scope"
            f" HAVING COUNT(DISTINCT ci.callee_name) = {len(members)}"
            " ORDER BY 1 LIMIT 1000",
            (*vids, *members, *source_params),
        ).fetchall()
        if not scope_rows:
            return []
        wanted_scopes = [r[0] for r in scope_rows]
        scope_marks = ",".join("?" for _ in wanted_scopes)
        labels = source_labels(self._conn, set(source_params))
        rows = self._conn.execute(
            "SELECT ci.version_id, l.name, v.version, ci.caller_file, ci.caller,"
            " ci.callee_name, ci.args, ci.line_start, ci.line_end, ci.confidence,"
            " ci.call_order, ci.start_byte, ci.id, ci.resolve_kind,"
            " ci.source_id, ci.sample_id, ci.source_role, ci.origin_kind,"
            " ci.target_status, ci.relation_kind, ci.macro_host,"
            " ci.macro_host_file, ci.macro_definition_line, ci.macro_conditions"
            " FROM call_instances ci LEFT JOIN review_status rs "
            "ON rs.entity_type='call_instance' AND rs.entity_id=ci.id"
            " JOIN versions v ON v.id = ci.version_id"
            " JOIN libraries l ON l.id = v.library_id"
            f" WHERE ci.version_id IN ({placeholders}) {conf}"
            f" AND ci.in_target_api=1 AND ci.source_id IN ({source_marks})"
            f" AND ci.scope IN ({scope_marks})"
            " ORDER BY ci.version_id, ci.caller_file, ci.line_start,"
            " ci.start_byte, ci.call_order, ci.id",
            (*vids, *source_params, *wanted_scopes),
        ).fetchall()
        groups: dict[tuple, dict[str, Any]] = {}
        order: list[tuple] = []
        for (
            vid,
            lib,
            ver,
            path,
            caller,
            callee,
            args,
            ls,
            le,
            cf,
            _o,
            _s,
            _i,
            rk,
            source_id,
            sample_id,
            source_role,
            origin_kind,
            target_status,
            relation_kind,
            macro_host,
            macro_host_file,
            macro_definition_line,
            macro_conditions,
        ) in rows:
            if macro_policy == "exclude" and str(callee) in macro_names:
                continue
            key = (vid, path, caller or "")
            label = labels.get(int(source_id or 0), {})
            slot = groups.get(key)
            if slot is None:
                profile_id = next(iter(_profiles), 0)
                slot = {
                    "library": lib,
                    "version": ver,
                    "caller_file": path,
                    "caller": caller,
                    "sample_id": str(sample_id or ""),
                    "source_role": str(source_role or ""),
                    "origin_kind": str(origin_kind or ""),
                    "source_key": str(label.get("source_key") or ""),
                    "source_url": str(label.get("source_url") or ""),
                    "heading": str(label.get("heading") or ""),
                    "profile": str(_profiles.get(profile_id, {}).get("name") or ""),
                    "profile_fingerprint": str(
                        _profiles.get(profile_id, {}).get("fingerprint") or ""
                    ),
                    "source_hint": str(
                        _profiles.get(profile_id, {}).get("source_hint") or ""
                    ),
                    "macro_policy": macro_policy,
                    "declared_target_version": str(
                        label.get("declared_target_version") or ""
                    ),
                    "source_commit": str(label.get("commit_sha") or ""),
                    "canonical_url": "",
                    "target_status_counts": {},
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
                    "resolve_kind": rk,
                    "target_status": str(target_status or ""),
                    "relation_kind": str(relation_kind or "direct"),
                    "macro_body": str(relation_kind or "") == "macro",
                    "macro_host": str(macro_host or ""),
                    "definition_location": (
                        f"{macro_host_file or path}:{macro_definition_line}"
                        if macro_host_file or macro_definition_line
                        else ""
                    ),
                    "definition_url": (
                        canonical_url(
                            str(label.get("source_url") or ""),
                            str(label.get("commit_sha") or ""),
                            str(macro_host_file or path),
                            int(macro_definition_line or 0),
                            int(macro_definition_line or 0),
                        )
                        if str(relation_kind or "") == "macro"
                        else ""
                    ),
                    "macro_conditions": _parse_json_list(macro_conditions),
                }
            )
        self._attach_signatures(list(groups.values()), vids)
        out = []
        for key in order:
            calls = groups[key]["calls"]
            names = [c["callee"] for c in calls]
            matched = subsequence_positions(pattern, names)
            if not matched:
                continue
            groups[key]["matched"] = matched
            lines = [int(call["line_start"]) for call in calls if call["line_start"]]
            ends = [int(call["line_end"]) for call in calls if call["line_end"]]
            groups[key]["canonical_url"] = canonical_url(
                str(groups[key].get("source_url") or ""),
                str(groups[key].get("source_commit") or ""),
                str(groups[key].get("caller_file") or ""),
                min(lines) if lines else 0,
                max(ends) if ends else 0,
            )
            groups[key]["target_status_counts"] = {
                status: sum(1 for call in calls if call["target_status"] == status)
                for status in sorted({call["target_status"] for call in calls})
            }
            groups[key]["rendered"] = render_evidence(groups[key], calls, matched)
            out.append(groups[key])
        from libref.config import setting_or_default
        from libref.mining.idiom import deduplicate_evidence, synthesize_snippet

        file_cap = (
            per_file_limit
            if per_file_limit is not None
            else int(setting_or_default(self._settings, "evidence_per_file_limit"))
        )
        do_dedup = (
            dedup
            if dedup is not None
            else bool(setting_or_default(self._settings, "evidence_dedup"))
        )
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

    def _sample_patterns(
        self, pids: list[int], cap: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Read a bounded random pattern sample and its population count."""
        if not pids:
            return [], 0
        placeholders = ",".join("?" for _ in pids)
        where = f" WHERE profile_id IN ({placeholders})"
        total = int(
            self._conn.execute(
                "SELECT count(*) FROM seq_patterns" + where, tuple(pids)
            ).fetchone()[0]
        )
        if total <= cap:
            return self.list_patterns(limit=cap, _ctx=(pids, set(), {})), total
        rows = self._conn.execute(
            "SELECT pattern, support FROM seq_patterns"
            + where
            + " ORDER BY random() LIMIT ?",
            (*pids, cap),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for raw, support in rows:
            try:
                names = json.loads(raw)
            except TypeError, ValueError:
                continue
            out.append({"pattern": names, "support": support})
        return out, total

    def signatures_for(
        self,
        apis: list[str],
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids or not apis:
            return {}
        placeholders = ",".join("?" for _ in vids)
        marks = ",".join("?" for _ in apis)
        rows = self._conn.execute(
            "SELECT qualified_name, kind, signature, params, return_type"
            " FROM api_entities"
            f" WHERE version_id IN ({placeholders}) AND qualified_name IN ({marks})"
            " ORDER BY qualified_name, kind",
            (*vids, *apis),
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for qual, kind, sig, params, ret in rows:
            out.setdefault(str(qual), {})[str(kind)] = {
                "signature": sig or "",
                "params": _parse_json_list(params),
                "return_type": ret or "",
            }
        return out

    def canonical_signature(
        self,
        api: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Single deterministic assembly shared by evidence and induction."""
        from libref.mining.idiom import format_short_signature

        table = self.signatures_for(
            [api], library, version, ecosystem, tenant_id=tenant_id
        )
        by_kind = table.get(str(api)) or {}
        if not by_kind:
            return {"api": api, "kind": "", "signature": "", "short": api + "()"}
        rows = [
            (
                kind,
                str(info.get("signature") or ""),
                list(info.get("params") or []),
                str(info.get("return_type") or ""),
            )
            for kind, info in by_kind.items()
        ]
        kind, sig, params, ret = choose_canonical(rows)
        return {
            "api": api,
            "kind": kind,
            "signature": sig,
            "params": params,
            "return_type": ret,
            "short": format_short_signature(api, params, ret),
        }

    def _attach_signatures(self, groups: list[dict[str, Any]], vids: list[int]) -> None:
        wanted: set[str] = set()
        for group in groups:
            for call in group.get("calls") or []:
                name = str(call.get("callee") or "")
                if name:
                    wanted.add(name)
            caller = str(group.get("caller") or "")
            if caller:
                wanted.add(caller)
        if not wanted:
            return
        placeholders = ",".join("?" for _ in vids)
        marks = ",".join("?" for _ in sorted(wanted))
        try:
            rows = self._conn.execute(
                "SELECT qualified_name, kind, signature, params, return_type FROM api_entities"
                f" WHERE version_id IN ({placeholders}) AND qualified_name IN ({marks})"
                " ORDER BY qualified_name, kind",
                (*vids, *sorted(wanted)),
            ).fetchall()
        except Exception:  # noqa: BLE001 -- signature join is advisory for evidence rendering
            return
        bucket: dict[str, list[tuple[str, str, list, str]]] = {}
        for qual, kind, sig, params, ret in rows:
            bucket.setdefault(str(qual), []).append(
                (
                    str(kind or ""),
                    str(sig or ""),
                    _parse_json_list(params),
                    str(ret or ""),
                )
            )
        info: dict[str, dict[str, Any]] = {}
        for qual, items in bucket.items():
            kind, sig, params, ret = choose_canonical(items)
            info[qual] = {
                "kind": kind,
                "signature": sig,
                "params": params,
                "return_type": ret,
            }
        for group in groups:
            for call in group.get("calls") or []:
                hit = info.get(str(call.get("callee") or ""))
                if hit and hit["signature"]:
                    call["callee_signature"] = hit["signature"]
                if hit and hit["params"]:
                    call["callee_params"] = hit["params"]
                if hit and hit["return_type"]:
                    call["callee_return_type"] = hit["return_type"]
            caller = str(group.get("caller") or "")
            hit = info.get(caller) if caller else None
            if hit and hit["signature"]:
                group["caller_signature"] = hit["signature"]
            if hit and hit["params"]:
                group["caller_params"] = hit["params"]
