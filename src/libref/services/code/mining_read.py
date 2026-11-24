"""Mining read model: call frequency, co-occurrence, patterns, trees, clusters."""

from __future__ import annotations

import json
from typing import Any

from libref.astminer.models import TARGET_STATUS_PREFIX
from libref.limits import INSIGHT_ROWS, PER_FILE_ROWS, require_limit
from libref.services.code.mining_evidence import (
    canonical_url,
    choose_canonical,
    kind_rank,
    render_evidence,
    source_labels,
    subsequence_positions,
)
from libref.services.code.usage_sources import profile_source_context
from libref.services.registry.libraries import mining_version_ids

ProfileContext = tuple[list[int], set[int], dict[int, dict[str, Any]]]


def _profile_context(
    conn, version_ids: list[int], profile: str | None = None
) -> tuple[list[int], set[int], dict[int, dict[str, str]]]:
    """Resolve profile identities, sources, and audit metadata for read paths."""
    if not version_ids:
        return [], set(), {}
    name = profile or "usage"
    placeholders = ",".join("?" for _ in version_ids)
    rows = conn.execute(
        f"SELECT id, name, source_ids, fingerprint, params FROM mining_profiles"
        f" WHERE version_id IN ({placeholders}) AND name=?",
        (*version_ids, name),
    ).fetchall()
    ids = [int(r[0]) for r in rows]
    sources: set[int] = set()
    details: dict[int, dict[str, Any]] = {}
    for row in rows:
        raw = row[2]
        if not raw:
            members: list[int] = []
        else:
            try:
                values = json.loads(str(raw))
            except ValueError:
                values = []
            members = [int(v) for v in values] if isinstance(values, list) else []
        sources.update(members)
        context = profile_source_context(conn, members)
        try:
            raw_params = json.loads(str(row[4])) if row[4] else {}
        except ValueError:
            raw_params = {}
        policy = str((raw_params or {}).get("macro_policy") or "exclude")
        hint = context["hint"]
        if policy in ("review_only", "approved_only"):
            hint += (
                f"; macro policy {policy}; macro calls are visible in macro bodies,"
                " not expanded call sites"
            )
        details[int(row[0])] = {
            "name": str(row[1]),
            "fingerprint": str(row[3] or ""),
            "source_ids": members,
            "source_roles": context["roles"],
            "source_hint": hint,
            "macro_policy": policy,
        }
    return ids, sources, details


def _source_scope_sql(
    source_ids: list[int] | tuple[int, ...] | set[int] | None,
) -> tuple[str, list[int]]:
    """Build a source predicate; an empty profile scope matches no calls."""
    if source_ids is None:
        return "", []
    ids = sorted({int(value) for value in source_ids})
    if not ids:
        return " AND 1=0", []
    marks = ",".join("?" for _ in ids)
    return f" AND source_id IN ({marks})", ids


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


def _parse_json_list(raw: Any) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return list(raw)
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return list(parsed) if isinstance(parsed, list) else []


def _call_filter_sql(
    alias: str,
    include_ambiguous: bool,
    resolve_kinds: list[str] | tuple[str, ...] | None,
    min_args: int | None,
    max_args: int | None,
    relation_kinds: list[str] | tuple[str, ...] | None = None,
    macro_policy: str = "exclude",
) -> tuple[str, list[Any]]:
    """Shared call filter with review semantics; empty inputs keep history."""
    parts: list[str] = ["(rs.status IS NULL OR rs.status != 'conflict')"]
    params: list[Any] = []
    parts.append(
        f"COALESCE({alias}.target_status,'resolved_target') != 'version_unconfirmed'"
    )
    if not include_ambiguous:
        parts.append(
            f"(COALESCE({alias}.target_status,'resolved_target') !='prefix_candidate'"
            " OR rs.status='approved')"
        )
        parts.append(f"({alias}.confidence != 'AMBIGUOUS' OR rs.status='approved')")
        # Stdlib/builtin references are certain externals (INFERRED), so the
        # exclusion that once rode on AMBIGUOUS now rides on the source column.
        parts.append(
            f"(COALESCE({alias}.resolve_kind,'') NOT IN ('stdlib','builtin')"
            " OR rs.status='approved')"
        )
    if resolve_kinds:
        wanted = sorted({str(k) for k in resolve_kinds if str(k).strip()})
        if wanted:
            marks = ",".join("?" for _ in wanted)
            parts.append(f"({alias}.resolve_kind IN ({marks}))")
            params.extend(wanted)
    relations = sorted({str(k) for k in (relation_kinds or []) if str(k).strip()})
    if macro_policy == "exclude" and "macro" in relations:
        macro_policy = "review_only"
    if relations:
        marks = ",".join("?" for _ in relations)
        parts.append(f"(COALESCE({alias}.relation_kind,'direct') IN ({marks}))")
        params.extend(relations)
    elif macro_policy == "review_only":
        pass
    elif macro_policy == "approved_only":
        parts.append(
            f"(COALESCE({alias}.relation_kind,'direct') != 'macro'"
            " OR rs.status='approved')"
        )
    else:
        # Macro shape is excluded independently of the ambiguous-call switch.
        parts.append(f"COALESCE({alias}.relation_kind,'direct') != 'macro'")
    if min_args is not None:
        parts.append(f"({alias}.argument_count >= ?)")
        params.append(int(min_args))
    if max_args is not None:
        parts.append(f"({alias}.argument_count <= ?)")
        params.append(int(max_args))
    if not parts:
        return "", []
    return " AND " + " AND ".join(parts), params


def _sibling_filter(alias: str, review: str, include_ambiguous: bool) -> str:
    """Review and confidence filter shared by single and batched sibling reads."""
    base = f"AND ({review}.status IS NULL OR {review}.status != 'conflict')"
    if include_ambiguous:
        return base
    return (
        base
        + f" AND ({alias}.confidence != 'AMBIGUOUS' OR {review}.status='approved')"
        + f" AND (COALESCE({alias}.resolve_kind,'') NOT IN ('stdlib','builtin')"
        + f" OR {review}.status='approved')"
    )


class MiningReadService:
    """Read-only queries over call_instances, seq_patterns, and derived tables."""

    def __init__(self, conn, settings=None) -> None:
        self._conn = conn
        self._settings = settings

    def api_usage_counts(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int | None = None,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        base_filter = "(rs.status IS NULL OR rs.status != 'conflict')"
        if include_ambiguous:
            conf = f"AND {base_filter}"
        else:
            conf = (
                f"AND {base_filter} "
                "AND (call_instances.confidence != 'AMBIGUOUS'"
                " OR rs.status='approved')"
                " AND (COALESCE(call_instances.resolve_kind,'') NOT IN ('stdlib','builtin')"
                " OR rs.status='approved')"
            )
        sql = (
            "SELECT callee_name, count(*), "
            f"count(DISTINCT call_instances.scope) "
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
        tenant_id: str | None = None,
        limit: int = 10,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        base_filter = "(rs.status IS NULL OR rs.status != 'conflict')"
        if include_ambiguous:
            conf = f"AND {base_filter}"
        else:
            conf = (
                f"AND {base_filter} "
                "AND (call_instances.confidence != 'AMBIGUOUS'"
                " OR rs.status='approved')"
                " AND (COALESCE(call_instances.resolve_kind,'') NOT IN ('stdlib','builtin')"
                " OR rs.status='approved')"
            )
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
        tenant_id: str | None = None,
        limit: int = 10,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        conf = _sibling_filter("c", "rc", include_ambiguous)
        tconf = _sibling_filter("call_instances", "rt", include_ambiguous)
        rows = self._conn.execute(
            "WITH target(s) AS ("
            f" SELECT DISTINCT call_instances.scope"
            " FROM call_instances LEFT JOIN review_status rt "
            "ON rt.entity_type='call_instance' AND rt.entity_id=call_instances.id"
            f" WHERE version_id IN ({placeholders}) AND callee_name = ? {tconf})"
            " SELECT c.callee_name,"
            f" count(DISTINCT c.scope),"
            " count(*) FROM call_instances c LEFT JOIN review_status rc "
            "ON rc.entity_type='call_instance' AND rc.entity_id=c.id"
            f" JOIN target t ON c.scope = t.s"
            f" WHERE c.version_id IN ({placeholders}) AND c.callee_name != ? {conf}"
            " GROUP BY c.callee_name"
            " ORDER BY 2 DESC, 3 DESC, c.callee_name LIMIT ?",
            (*vids, api, *vids, api, limit),
        ).fetchall()
        return [{"api": r[0], "scopes": r[1], "calls": r[2]} for r in rows]

    def related_apis_batch(
        self,
        apis: list[str] | tuple[str, ...] | set[str],
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 4,
        include_ambiguous: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """Sibling APIs for every requested API in one grouped query."""
        wanted = sorted({str(api) for api in apis if api})
        if not wanted:
            return {}
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return {api: [] for api in wanted}
        placeholders = ",".join("?" for _ in vids)
        marks = ",".join("?" for _ in wanted)
        conf = _sibling_filter("c", "rc", include_ambiguous)
        tconf = _sibling_filter("call_instances", "rt", include_ambiguous)
        rows = self._conn.execute(
            "WITH target(api, s) AS ("
            f" SELECT DISTINCT call_instances.callee_name, call_instances.scope"
            " FROM call_instances LEFT JOIN review_status rt "
            "ON rt.entity_type='call_instance' AND rt.entity_id=call_instances.id"
            f" WHERE version_id IN ({placeholders}) AND callee_name IN ({marks}) {tconf})"
            " SELECT t.api, c.callee_name, count(DISTINCT c.scope), count(*)"
            " FROM call_instances c LEFT JOIN review_status rc "
            "ON rc.entity_type='call_instance' AND rc.entity_id=c.id"
            " JOIN target t ON c.scope = t.s"
            f" WHERE c.version_id IN ({placeholders}) {conf}"
            " GROUP BY t.api, c.callee_name"
            " ORDER BY t.api, 3 DESC, 4 DESC, c.callee_name",
            (*vids, *wanted, *vids),
        ).fetchall()
        out: dict[str, list[dict[str, Any]]] = {api: [] for api in wanted}
        for api, sibling, scopes, calls in rows:
            api, sibling = str(api), str(sibling)
            if api == sibling or len(out[api]) >= limit:
                continue
            out[api].append({"api": sibling, "scopes": scopes, "calls": calls})
        return out

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

    def related_common(
        self,
        apis: list[str],
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 10,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        if not apis:
            return []
        wanted = sorted({a for a in apis if a})
        if not wanted:
            return []
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        base_filter = "(rs.status IS NULL OR rs.status != 'conflict')"
        if include_ambiguous:
            conf = f"AND {base_filter}"
        else:
            conf = (
                f"AND {base_filter} "
                "AND (call_instances.confidence != 'AMBIGUOUS'"
                " OR rs.status='approved')"
                " AND (COALESCE(call_instances.resolve_kind,'') NOT IN ('stdlib','builtin')"
                " OR rs.status='approved')"
            )
        placeholders_w = ",".join("?" for _ in wanted)
        no_conflict_inner = "(rh2.status IS NULL OR rh2.status != 'conflict')"
        no_conflict_outer = "(rh.status IS NULL OR rh.status != 'conflict')"
        if include_ambiguous:
            inner_conf = f"AND {no_conflict_inner}"
            outer_conf = f"AND {no_conflict_outer}"
        else:
            inner_conf = (
                f"AND {no_conflict_inner} "
                "AND (h2.confidence != 'AMBIGUOUS' OR rh2.status='approved')"
                " AND (h2.resolve_kind NOT IN ('stdlib','builtin')"
                " OR rh2.status='approved')"
            )
            outer_conf = (
                f"AND {no_conflict_outer} "
                "AND (h.confidence != 'AMBIGUOUS' OR rh.status='approved')"
                " AND (COALESCE(h.resolve_kind,'') NOT IN ('stdlib','builtin')"
                " OR rh.status='approved')"
            )
        hit_rows = self._conn.execute(
            f"SELECT h.scope, h.callee_name"
            " FROM call_instances h LEFT JOIN review_status rh"
            " ON rh.entity_type='call_instance' AND rh.entity_id=h.id"
            f" JOIN (SELECT h2.scope AS hk"
            " FROM call_instances h2 LEFT JOIN review_status rh2"
            " ON rh2.entity_type='call_instance' AND rh2.entity_id=h2.id"
            f" WHERE h2.version_id IN ({placeholders}) AND h2.callee_name IN ({placeholders_w}) {inner_conf}"
            " GROUP BY hk"
            f" HAVING COUNT(DISTINCT h2.callee_name) = {len(wanted)})"
            f" ON h.scope = hk"
            f" WHERE h.version_id IN ({placeholders}) {outer_conf}",
            (*vids, *wanted, *vids),
        ).fetchall()
        if not hit_rows:
            return []
        scopes: dict[str, set[str]] = {}
        for scope, callee in hit_rows:
            scopes.setdefault(str(scope), set()).add(str(callee))
        wanted_set = set(wanted)
        counter: dict[str, dict[str, int]] = {}
        for have in scopes.values():
            for name in have:
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

    def _sample_clusters(
        self, pids: list[int], cap: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Read a bounded random cluster sample and its population count."""
        if not pids:
            return [], 0
        placeholders = ",".join("?" for _ in pids)
        where = f" WHERE profile_id IN ({placeholders})"
        total = int(
            self._conn.execute(
                "SELECT count(DISTINCT cluster_id) FROM api_clusters" + where,
                tuple(pids),
            ).fetchone()[0]
        )
        ids = [
            int(row[0])
            for row in self._conn.execute(
                "SELECT DISTINCT cluster_id FROM api_clusters"
                + where
                + " ORDER BY random() LIMIT ?",
                (*pids, cap),
            ).fetchall()
        ]
        if not ids:
            return [], total
        marks = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            "SELECT cluster_id, member, size, representative FROM api_clusters"
            f" WHERE profile_id IN ({placeholders}) AND cluster_id IN ({marks})"
            " ORDER BY cluster_id, member",
            (*pids, *ids),
        ).fetchall()
        grouped: dict[int, dict[str, Any]] = {}
        for cid, member, size, rep in rows:
            slot = grouped.setdefault(
                int(cid),
                {
                    "cluster_id": int(cid),
                    "members": [],
                    "size": int(size),
                    "representative": rep,
                },
            )
            slot["members"].append(member)
        return [grouped[cid] for cid in ids if cid in grouped], total

    def mining_health(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        profile: str | None = None,
        *,
        _ctx: ProfileContext | None = None,
    ) -> dict[str, Any]:
        from libref.mining.idiom import reuse_diversity

        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        ctx = _ctx or _profile_context(self._conn, vids, profile)
        pids, sources, _profiles = ctx
        patterns, pattern_total = self._sample_patterns(pids, INSIGHT_ROWS)
        clusters, cluster_total = self._sample_clusters(pids, INSIGHT_ROWS)
        health = reuse_diversity(patterns, clusters)
        health["sampling"] = {
            "patterns": {
                "sample": len(patterns),
                "population": pattern_total,
                "truncated": len(patterns) < pattern_total,
            },
            "clusters": {
                "sample": len(clusters),
                "population": cluster_total,
                "truncated": len(clusters) < cluster_total,
            },
        }
        scope = sorted(sources)
        health["resolve_kinds"] = self.resolve_distribution(
            library, version, ecosystem, tenant_id=tenant_id, source_ids=scope
        )
        health["confidence"] = self.confidence_distribution(
            library, version, ecosystem, tenant_id=tenant_id, source_ids=scope
        )
        health.update(
            self.quality_rates(
                library, version, ecosystem, tenant_id=tenant_id, source_ids=scope
            )
        )
        if scope:
            marks = ",".join("?" for _ in scope)
            rows = self._conn.execute(
                "SELECT COALESCE(target_status,'resolved_target'), count(*)"
                " FROM call_instances WHERE version_id IN ("
                + ",".join("?" for _ in vids)
                + f") AND source_id IN ({marks}) AND in_target_api=1"
                " GROUP BY 1 ORDER BY 1",
                (*vids, *scope),
            ).fetchall()
            health["target_status"] = {str(k): int(v) for k, v in rows}
            total = sum(health["target_status"].values())
            health["prefix_candidate_rate"] = (
                round(health["target_status"].get(TARGET_STATUS_PREFIX, 0) / total, 4)
                if total
                else 0.0
            )
        else:
            health["target_status"] = {}
            health["prefix_candidate_rate"] = 0.0
        if sources:
            marks = ",".join("?" for _ in sources)
            rows = self._conn.execute(
                "SELECT COALESCE(relation_kind,'direct'), count(*)"
                " FROM call_instances WHERE version_id IN ("
                + ",".join("?" for _ in vids)
                + f") AND source_id IN ({marks}) GROUP BY 1",
                (*vids, *sorted(sources)),
            ).fetchall()
            relations = {str(kind): int(count) for kind, count in rows}
            total = sum(relations.values())
            macro = relations.get("macro", 0)
            health["relations"] = relations
            macro_entities = {
                str(row[0])
                for row in self._conn.execute(
                    "SELECT qualified_name FROM api_entities WHERE version_id IN ("
                    + ",".join("?" for _ in vids)
                    + ") AND kind='macro'",
                    tuple(vids),
                ).fetchall()
            }
            macro_rows = self._conn.execute(
                "SELECT callee_name, confidence, COALESCE(target_status,''),"
                " COALESCE(resolve_kind,''), count(*) FROM call_instances"
                " WHERE version_id IN ("
                + ",".join("?" for _ in vids)
                + f") AND source_id IN ({marks}) AND relation_kind='macro'"
                " GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC, 1",
                (*vids, *sorted(sources)),
            ).fetchall()
            macro_calls = sum(int(row[4]) for row in macro_rows)
            resolved_macro_calls = sum(
                int(row[4]) for row in macro_rows if str(row[0]) in macro_entities
            )
            covered = {str(row[0]) for row in macro_rows} & macro_entities
            confidence: dict[str, int] = {}
            unresolved: list[dict[str, Any]] = []
            for name, conf, status, kind, count in macro_rows:
                name = str(name)
                confidence[str(conf)] = confidence.get(str(conf), 0) + int(count)
                if (
                    name not in macro_entities
                    or status in (TARGET_STATUS_PREFIX,)
                    or kind == "unresolved"
                ):
                    unresolved.append(
                        {
                            "api": name,
                            "calls": int(count),
                            "target_status": str(status),
                            "resolve_kind": str(kind),
                        }
                    )
            target_calls = sum(health.get("target_status", {}).values())
            health["macro_coverage"] = {
                "macro": macro,
                "total": total,
                "ratio": round(macro / total, 4) if total else 0.0,
                "macro_calls": macro_calls,
                "target_calls": target_calls,
                "target_macro_ratio": (
                    round(macro_calls / target_calls, 4) if target_calls else 0.0
                ),
                "target_macro_definitions": len(macro_entities),
                "covered_macro_definitions": len(covered),
                "definition_coverage": (
                    round(len(covered) / len(macro_entities), 4)
                    if macro_entities
                    else 0.0
                ),
                "resolved_macro_calls": resolved_macro_calls,
                "macro_resolution_ratio": (
                    round(resolved_macro_calls / macro_calls, 4) if macro_calls else 0.0
                ),
                "confidence": confidence,
                "unresolved_macro_calls": sum(item["calls"] for item in unresolved),
                "unresolved": unresolved,
            }
        health["facet_counts"] = self._facet_counts(pids)
        return health

    def _facet_counts(self, pids: list[int]) -> dict[str, int]:
        if not pids or not self._table_present("feature_facets"):
            return {}
        placeholders = ",".join("?" for _ in pids)
        try:
            rows = self._conn.execute(
                "SELECT facet, count(*) FROM feature_facets"
                f" WHERE profile_id IN ({placeholders}) GROUP BY facet",
                tuple(pids),
            ).fetchall()
        except Exception:  # noqa: BLE001 -- health must not fail on odd backends
            return {}
        return {str(facet): int(count) for facet, count in rows}

    def _arg_shapes_with_facets(
        self,
        api: str,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        from libref.mining.facets import mask_call_shape

        rows = self.arg_shapes(
            api,
            library=library,
            version=version,
            ecosystem=ecosystem,
            limit=limit,
            tenant_id=tenant_id,
        )
        known = self._facet_weights_for_api(api, library, version, ecosystem)
        for row in rows:
            value = mask_call_shape(api, row.get("args"))
            match = known.get(value)
            row["facet"] = value
            row["facet_calls"] = int(match[0]) if match else 0
            row["facet_scopes"] = int(match[1]) if match else 0
        return rows

    def _facet_weights_for_api(
        self,
        api: str,
        library: str | None,
        version: str | None,
        ecosystem: str | None,
    ) -> dict[str, tuple[int, int]]:
        """Version-wide arg-shape facet rows for one API, like arg_shapes."""
        from libref.mining.facets import FACET_ARG_SHAPE

        vids = mining_version_ids(self._conn, library, version, ecosystem)
        if not vids or not self._table_present("feature_facets"):
            return {}
        placeholders = ",".join("?" for _ in vids)
        try:
            rows = self._conn.execute(
                "SELECT value, max(calls), max(scopes) FROM feature_facets"
                f" WHERE version_id IN ({placeholders}) AND facet=?"
                " AND (value=? OR value LIKE ? ESCAPE '\\')"
                " GROUP BY value",
                (*vids, FACET_ARG_SHAPE, f"{api}(", f"{api}(%"),
            ).fetchall()
        except Exception:  # noqa: BLE001 -- facet tags are advisory
            return {}
        return {str(value): (int(calls), int(scopes)) for value, calls, scopes in rows}

    def resolve_distribution(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        source_ids: list[int] | tuple[int, ...] | set[int] | None = None,
    ) -> dict[str, int]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return {}
        placeholders = ",".join("?" for _ in vids)
        scope_sql, scope_params = _source_scope_sql(source_ids)
        rows = self._conn.execute(
            "SELECT resolve_kind, count(*) FROM call_instances"
            f" WHERE version_id IN ({placeholders}){scope_sql}"
            " GROUP BY resolve_kind ORDER BY resolve_kind",
            (*vids, *scope_params),
        ).fetchall()
        return {str(k or ""): int(c) for k, c in rows}

    def confidence_distribution(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        source_ids: list[int] | tuple[int, ...] | set[int] | None = None,
    ) -> dict[str, int]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return {}
        placeholders = ",".join("?" for _ in vids)
        scope_sql, scope_params = _source_scope_sql(source_ids)
        rows = self._conn.execute(
            "SELECT confidence, count(*) FROM call_instances"
            f" WHERE version_id IN ({placeholders}){scope_sql}"
            " GROUP BY confidence ORDER BY confidence",
            (*vids, *scope_params),
        ).fetchall()
        return {str(k or ""): int(c) for k, c in rows}

    def quality_rates(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        source_ids: list[int] | tuple[int, ...] | set[int] | None = None,
    ) -> dict[str, Any]:
        """Quality face: unresolved share, caller linkage, missing signatures."""
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return {
                "unresolved_rate": 0.0,
                "caller_link_rate": 0.0,
                "signature_missing_rate": 0.0,
            }
        placeholders = ",".join("?" for _ in vids)
        scope_sql, scope_params = _source_scope_sql(source_ids)
        params = (*vids, *scope_params)
        total_n = int(
            self._conn.execute(
                "SELECT count(*) FROM call_instances"
                f" WHERE version_id IN ({placeholders}){scope_sql}",
                params,
            ).fetchone()[0]
        )
        unresolved_n = int(
            self._conn.execute(
                "SELECT count(*) FROM call_instances"
                f" WHERE version_id IN ({placeholders}){scope_sql}"
                " AND resolve_kind='unresolved'",
                params,
            ).fetchone()[0]
        )
        linked_n = int(
            self._conn.execute(
                "SELECT count(*) FROM call_instances"
                f" WHERE version_id IN ({placeholders}){scope_sql}"
                " AND caller_entity_id IS NOT NULL",
                params,
            ).fetchone()[0]
        )
        entity_exists = (
            "EXISTS (SELECT 1 FROM api_entities ae WHERE ae.version_id=ci.version_id"
            " AND ae.qualified_name=ci.callee_name)"
        )
        signature_missing = (
            "NOT EXISTS (SELECT 1 FROM api_entities ae WHERE ae.version_id=ci.version_id"
            " AND ae.qualified_name=ci.callee_name"
            " AND ae.signature IS NOT NULL AND ae.signature != '')"
        )
        target_call_sql = (
            "SELECT count(*) FROM call_instances ci"
            f" WHERE ci.version_id IN ({placeholders}){scope_sql}"
            " AND ci.in_target_api=1"
        )
        sym_n = int(
            self._conn.execute(
                target_call_sql + f" AND {entity_exists}", params
            ).fetchone()[0]
        )
        missing_n = int(
            self._conn.execute(
                target_call_sql + f" AND {signature_missing}", params
            ).fetchone()[0]
        )
        return {
            "unresolved_rate": (unresolved_n / total_n) if total_n else 0.0,
            "caller_link_rate": (linked_n / total_n) if total_n else 0.0,
            "signature_missing_rate": (missing_n / sym_n) if sym_n else 0.0,
        }

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

    def importers_of(
        self,
        target: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids or not target:
            return []
        placeholders = ",".join("?" for _ in vids)
        rows = self._conn.execute(
            "SELECT importer_file, alias, target, kind FROM import_edges"
            f" WHERE version_id IN ({placeholders}) AND target = ?"
            " ORDER BY importer_file, alias LIMIT ? OFFSET ?",
            (*vids, target, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return [
            {"importer_file": r[0], "alias": r[1], "target": r[2], "kind": r[3]}
            for r in rows
        ]

    def imports_of_file(
        self,
        importer_file: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids or not importer_file:
            return []
        placeholders = ",".join("?" for _ in vids)
        rows = self._conn.execute(
            "SELECT importer_file, alias, target, kind FROM import_edges"
            f" WHERE version_id IN ({placeholders}) AND importer_file = ?"
            " ORDER BY alias LIMIT ? OFFSET ?",
            (*vids, importer_file, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return [
            {"importer_file": r[0], "alias": r[1], "target": r[2], "kind": r[3]}
            for r in rows
        ]

    def _symbol_dict(self, row: Any) -> dict[str, Any]:
        return {
            "qualified_name": str(row[0] or ""),
            "kind": str(row[1] or ""),
            "parent": row[2],
            "signature": str(row[3] or ""),
            "params": _parse_json_list(row[4]),
            "bases": _parse_json_list(row[5]),
            "depth": int(row[6] or 0),
            "file": str(row[7] or ""),
            "line_start": int(row[8] or 1),
            "line_end": int(row[9] or 1),
            "param_types": _parse_json_list(row[10]),
            "return_type": str(row[11] or ""),
            "has_doc": bool(int(row[12] or 0)),
        }

    def symbol_details(
        self,
        apis: list[str],
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Batch symbol lookup with deterministic kind ordering."""
        wanted = sorted({str(a) for a in (apis or []) if str(a).strip()})
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids or not wanted:
            return []
        placeholders = ",".join("?" for _ in vids)
        marks = ",".join("?" for _ in wanted)
        rows = self._conn.execute(
            "SELECT qualified_name, kind, parent, signature, params, bases, depth,"
            " file, line_start, line_end, param_types, return_type, has_doc"
            " FROM api_entities"
            f" WHERE version_id IN ({placeholders}) AND qualified_name IN ({marks})"
            " ORDER BY qualified_name, kind",
            (*vids, *wanted),
        ).fetchall()
        out = [self._symbol_dict(r) for r in rows]
        out.sort(
            key=lambda d: (
                str(d.get("qualified_name") or ""),
                0 if str(d.get("signature") or "").strip() else 1,
                kind_rank(d.get("kind")),
                str(d.get("kind") or ""),
            )
        )
        return out[: max(1, int(limit))]

    def symbols_in_file(
        self,
        importer_file: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids or not importer_file:
            return []
        placeholders = ",".join("?" for _ in vids)
        rows = self._conn.execute(
            "SELECT qualified_name, kind, parent, signature, params, bases, depth,"
            " file, line_start, line_end, param_types, return_type, has_doc"
            " FROM api_entities"
            f" WHERE version_id IN ({placeholders}) AND file = ?"
            " ORDER BY line_start, start_byte, qualified_name, kind LIMIT ? OFFSET ?",
            (*vids, importer_file, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return [self._symbol_dict(r) for r in rows]

    def child_symbols(
        self,
        parent: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids or not parent:
            return []
        placeholders = ",".join("?" for _ in vids)
        rows = self._conn.execute(
            "SELECT qualified_name, kind, parent, signature, params, bases, depth,"
            " file, line_start, line_end, param_types, return_type, has_doc"
            " FROM api_entities"
            f" WHERE version_id IN ({placeholders}) AND parent = ?"
            " ORDER BY qualified_name, kind LIMIT ? OFFSET ?",
            (*vids, parent, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return [self._symbol_dict(r) for r in rows]

    def _call_dict(self, row: Any) -> dict[str, Any]:
        return {
            "callee": str(row[0] or ""),
            "caller": row[1],
            "caller_file": str(row[2] or ""),
            "line_start": int(row[3] or 1),
            "line_end": int(row[4] or 1),
            "start_byte": int(row[5] or 0),
            "end_byte": int(row[6] or 0),
            "confidence": str(row[7] or ""),
            "resolve_kind": str(row[8] or ""),
            "receiver": row[9],
            "func": str(row[10] or ""),
            "args": _parse_json_list(row[11]),
            "argument_count": int(row[12] or 0),
            "call_order": int(row[13] or 0),
            "callee_entity_id": row[14],
            "caller_entity_id": row[15],
            "relation_kind": str(row[16] or "direct"),
            "target_status": str(row[17] or ""),
        }

    def calls_by_callee(
        self,
        callee: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_ambiguous: bool = False,
        resolve_kinds: list[str] | None = None,
        min_args: int | None = None,
        max_args: int | None = None,
        relation_kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Who calls me: deterministic caller listing for one callee."""
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids or not callee:
            return []
        placeholders = ",".join("?" for _ in vids)
        extra, params = _call_filter_sql(
            "call_instances",
            include_ambiguous,
            resolve_kinds,
            min_args,
            max_args,
            relation_kinds,
        )
        rows = self._conn.execute(
            "SELECT callee_name, caller, caller_file, line_start, line_end,"
            " start_byte, end_byte, confidence, resolve_kind, receiver, func, args,"
            " argument_count, call_order, callee_entity_id, caller_entity_id,"
            " COALESCE(call_instances.relation_kind,'direct'),"
            " COALESCE(call_instances.target_status,'resolved_target')"
            " FROM call_instances LEFT JOIN review_status rs"
            " ON rs.entity_type='call_instance' AND rs.entity_id=call_instances.id"
            f" WHERE version_id IN ({placeholders}) AND callee_name = ?{extra}"
            " ORDER BY caller_file, line_start, start_byte, call_order,"
            " call_instances.id"
            " LIMIT ? OFFSET ?",
            (*vids, callee, *params, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return [self._call_dict(r) for r in rows]

    def calls_by_caller(
        self,
        caller: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_ambiguous: bool = False,
        resolve_kinds: list[str] | None = None,
        min_args: int | None = None,
        max_args: int | None = None,
        relation_kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Who I call: deterministic callee listing for one caller."""
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids or not caller:
            return []
        placeholders = ",".join("?" for _ in vids)
        extra, params = _call_filter_sql(
            "call_instances",
            include_ambiguous,
            resolve_kinds,
            min_args,
            max_args,
            relation_kinds,
        )
        rows = self._conn.execute(
            "SELECT callee_name, caller, caller_file, line_start, line_end,"
            " start_byte, end_byte, confidence, resolve_kind, receiver, func, args,"
            " argument_count, call_order, callee_entity_id, caller_entity_id,"
            " COALESCE(call_instances.relation_kind,'direct'),"
            " COALESCE(call_instances.target_status,'resolved_target')"
            " FROM call_instances LEFT JOIN review_status rs"
            " ON rs.entity_type='call_instance' AND rs.entity_id=call_instances.id"
            f" WHERE version_id IN ({placeholders}) AND caller = ?{extra}"
            " ORDER BY caller_file, line_start, start_byte, call_order,"
            " call_instances.id"
            " LIMIT ? OFFSET ?",
            (*vids, caller, *params, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return [self._call_dict(r) for r in rows]

    def calls_in_file(
        self,
        importer_file: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        include_ambiguous: bool = False,
        resolve_kinds: list[str] | None = None,
        min_args: int | None = None,
        max_args: int | None = None,
        relation_kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids or not importer_file:
            return []
        placeholders = ",".join("?" for _ in vids)
        extra, params = _call_filter_sql(
            "call_instances",
            include_ambiguous,
            resolve_kinds,
            min_args,
            max_args,
            relation_kinds,
        )
        rows = self._conn.execute(
            "SELECT callee_name, caller, caller_file, line_start, line_end,"
            " start_byte, end_byte, confidence, resolve_kind, receiver, func, args,"
            " argument_count, call_order, callee_entity_id, caller_entity_id,"
            " COALESCE(call_instances.relation_kind,'direct'),"
            " COALESCE(call_instances.target_status,'resolved_target')"
            " FROM call_instances LEFT JOIN review_status rs"
            " ON rs.entity_type='call_instance' AND rs.entity_id=call_instances.id"
            f" WHERE version_id IN ({placeholders}) AND caller_file = ?{extra}"
            " ORDER BY line_start, start_byte, call_order, call_instances.id"
            " LIMIT ? OFFSET ?",
            (*vids, importer_file, *params, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return [self._call_dict(r) for r in rows]

    def list_labels(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        containing: list[str] | None = None,
        limit: int = 50,
        profile: str | None = None,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return []
        pids, _sources, profiles = _profile_context(self._conn, vids, profile)
        if not pids:
            return []
        placeholders = ",".join("?" for _ in pids)
        if hasattr(self._conn, "table_columns") and not self._conn.table_columns(
            "pattern_labels"
        ):
            return []
        try:
            rows = self._conn.execute(
                "SELECT profile_id, pattern, pattern_hash, model, prompt_version, name,"
                " summary, category_path, workflow, related_apis, updated_at"
                " FROM pattern_labels"
                f" WHERE profile_id IN ({placeholders})"
                " ORDER BY updated_at DESC, pattern_hash, model, prompt_version",
                tuple(pids),
            ).fetchall()
        except Exception:  # noqa: BLE001 -- labels are an optional derived table
            return []
        out = []
        seen: set[str] = set()
        for (
            pid,
            raw,
            phash,
            model,
            pver,
            name,
            summary,
            cat,
            workflow,
            related,
            _updated,
        ) in rows:
            if str(phash) in seen:
                continue
            seen.add(str(phash))
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
                    "profile": str(profiles.get(int(pid), {}).get("name") or ""),
                    "profile_fingerprint": str(
                        profiles.get(int(pid), {}).get("fingerprint") or ""
                    ),
                    "source_roles": list(
                        profiles.get(int(pid), {}).get("source_roles") or []
                    ),
                    "source_hint": str(
                        profiles.get(int(pid), {}).get("source_hint") or ""
                    ),
                    "macro_policy": str(
                        profiles.get(int(pid), {}).get("macro_policy") or "exclude"
                    ),
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
        tenant_id: str | None = None,
        prefix: str | None = None,
        limit: int = 100,
        profile: str | None = None,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return []
        pids, _sources, _profiles = _profile_context(self._conn, vids, profile)
        if not pids:
            return []
        placeholders = ",".join("?" for _ in pids)
        params: list[Any] = list(pids)
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
            f" WHERE profile_id IN ({placeholders}){filt}"
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
        tenant_id: str | None = None,
        containing: list[str] | None = None,
        limit: int = 50,
        profile: str | None = None,
        *,
        _ctx: ProfileContext | None = None,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return []
        pids = (_ctx or _profile_context(self._conn, vids, profile))[0]
        if not pids:
            return []
        placeholders = ",".join("?" for _ in pids)
        rows = self._conn.execute(
            "SELECT cluster_id, member, size, representative FROM api_clusters"
            f" WHERE profile_id IN ({placeholders})"
            " ORDER BY cluster_id, member",
            tuple(pids),
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

    def list_facets(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        facet: str | None = None,
        containing: list[str] | None = None,
        api: str | None = None,
        limit: int = 50,
        profile: str | None = None,
        *,
        _ctx: ProfileContext | None = None,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return []
        pids = (_ctx or _profile_context(self._conn, vids, profile))[0]
        if not pids:
            return []
        if not self._table_present("feature_facets"):
            return []
        placeholders = ",".join("?" for _ in pids)
        params: list[Any] = list(pids)
        filt = ""
        if facet:
            filt += " AND facet=?"
            params.append(facet)
        rows = self._conn.execute(
            "SELECT facet, value, calls, scopes FROM feature_facets"
            f" WHERE profile_id IN ({placeholders}){filt}"
            " ORDER BY calls DESC, value",
            params,
        ).fetchall()
        out = [
            {"facet": r[0], "value": r[1], "calls": r[2], "scopes": r[3]} for r in rows
        ]
        if api:
            from libref.mining.facets import facet_callee

            out = [r for r in out if facet_callee(str(r["value"])) == api]
        if containing:
            out = [r for r in out if all(n in str(r["value"]) for n in containing)]
        return out[:limit]

    def facet_summary(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int = 50,
        profile: str | None = None,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        ctx = _profile_context(self._conn, vids, profile)
        rows = self.list_facets(
            library,
            version,
            ecosystem,
            tenant_id,
            limit=max(limit, 500),
            profile=profile,
            _ctx=ctx,
        )
        by_facet: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_facet.setdefault(str(row["facet"]), []).append(row)
        summary = {
            facet: {
                "rows": len(values),
                "calls": sum(int(v["calls"]) for v in values),
                "top": values[:10],
            }
            for facet, values in sorted(by_facet.items())
        }
        health = self.mining_health(
            library,
            version,
            ecosystem,
            tenant_id=tenant_id,
            profile=profile,
            _ctx=ctx,
        )
        return [
            {
                "facets": summary,
                "patterns": health.get("patterns", 0),
                "total_support": health.get("total_support", 0),
                "reusability": health.get("reusability", 0.0),
                "diversity": health.get("diversity", 0.0),
            }
        ]

    def facet_metrics(
        self,
        facet: str,
        value: str,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        from libref.mining.facets import score_facet_row

        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        ctx = _profile_context(self._conn, vids, profile)
        rows = self.list_facets(
            library,
            version,
            ecosystem,
            tenant_id,
            facet=facet,
            limit=100000,
            profile=profile,
            _ctx=ctx,
        )
        target = next((r for r in rows if str(r["value"]) == value), None)
        if target is None:
            raise ValueError(f"unknown facet value: {facet}={value}")
        _pids, sources, _profiles = ctx
        total_calls = self._count_profile_calls(vids, sources)
        total_scopes = self._count_profile_scopes(vids, sources)
        return score_facet_row(
            facet,
            value,
            int(target["calls"]),
            int(target["scopes"]),
            peer_rows=rows,
            total_calls=total_calls,
            total_scopes=total_scopes,
        )

    def list_cluster_labels(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        containing: list[str] | None = None,
        limit: int = 50,
        profile: str | None = None,
    ) -> list[dict[str, Any]]:
        vids = mining_version_ids(self._conn, library, version, ecosystem, tenant_id)
        if not vids:
            return []
        pids, _sources, profiles = _profile_context(self._conn, vids, profile)
        if not pids:
            return []
        if not self._table_present("cluster_labels"):
            return []
        placeholders = ",".join("?" for _ in pids)
        rows = self._conn.execute(
            "SELECT profile_id, cluster_key, members, model, prompt_version, name,"
            " summary, category_path, workflow, related_apis, source_note, updated_at"
            " FROM cluster_labels"
            f" WHERE profile_id IN ({placeholders})"
            " ORDER BY updated_at DESC, cluster_key, model, prompt_version",
            tuple(pids),
        ).fetchall()
        out = []
        seen: set[str] = set()
        for (
            pid,
            key,
            raw,
            model,
            pver,
            name,
            summary,
            cat,
            workflow,
            related,
            note,
            _updated,
        ) in rows:
            if str(key) in seen:
                continue
            seen.add(str(key))
            try:
                members = json.loads(raw) if raw else []
            except ValueError:
                continue
            if containing and not all(n in members for n in containing):
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
                    "cluster_key": key,
                    "members": members,
                    "model": model,
                    "prompt_version": pver,
                    "name": name,
                    "summary": summary,
                    "category_path": cat_path,
                    "workflow": workflow,
                    "related_apis": rel,
                    "source_note": note,
                    "profile": str(profiles.get(int(pid), {}).get("name") or ""),
                }
            )
            if len(out) >= limit:
                break
        return out

    def profile_drift(
        self,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        profile: str | None = None,
        compare_profile: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Set-difference and support drift between two mining profiles."""
        from libref.config import setting_or_default

        if not compare_profile:
            raise ValueError("drift view requires compare_profile")
        cap = (
            int(limit)
            if limit is not None
            else int(setting_or_default(self._settings, "drift_topn"))
        )
        before = {
            json.dumps(p["pattern"], ensure_ascii=False): int(p["support"])
            for p in self.list_patterns(
                library,
                version,
                ecosystem,
                limit=100000,
                tenant_id=tenant_id,
                profile=profile,
            )
        }
        after = {
            json.dumps(p["pattern"], ensure_ascii=False): int(p["support"])
            for p in self.list_patterns(
                library,
                version,
                ecosystem,
                limit=100000,
                tenant_id=tenant_id,
                profile=compare_profile,
            )
        }
        changes: list[dict[str, Any]] = []
        for payload in sorted(set(after) - set(before)):
            changes.append(
                {
                    "kind": "pattern_added",
                    "pattern": json.loads(payload),
                    "support_before": 0,
                    "support_after": after[payload],
                    "delta": after[payload],
                }
            )
        for payload in sorted(set(before) - set(after)):
            changes.append(
                {
                    "kind": "pattern_removed",
                    "pattern": json.loads(payload),
                    "support_before": before[payload],
                    "support_after": 0,
                    "delta": -before[payload],
                }
            )
        for payload in sorted(set(before) & set(after)):
            delta = after[payload] - before[payload]
            if delta:
                changes.append(
                    {
                        "kind": "pattern_changed",
                        "pattern": json.loads(payload),
                        "support_before": before[payload],
                        "support_after": after[payload],
                        "delta": delta,
                    }
                )
        before_facets = {
            (str(r["facet"]), str(r["value"])): (int(r["calls"]), int(r["scopes"]))
            for r in self.list_facets(
                library,
                version,
                ecosystem,
                tenant_id,
                limit=100000,
                profile=profile,
            )
        }
        after_facets = {
            (str(r["facet"]), str(r["value"])): (int(r["calls"]), int(r["scopes"]))
            for r in self.list_facets(
                library,
                version,
                ecosystem,
                tenant_id,
                limit=100000,
                profile=compare_profile,
            )
        }
        for key in sorted(set(after_facets) - set(before_facets)):
            calls, _scopes = after_facets[key]
            changes.append(
                {
                    "kind": "facet_added",
                    "facet": key[0],
                    "value": key[1],
                    "calls_before": 0,
                    "calls_after": calls,
                    "delta": calls,
                }
            )
        for key in sorted(set(before_facets) - set(after_facets)):
            calls, _scopes = before_facets[key]
            changes.append(
                {
                    "kind": "facet_removed",
                    "facet": key[0],
                    "value": key[1],
                    "calls_before": calls,
                    "calls_after": 0,
                    "delta": -calls,
                }
            )
        for key in sorted(set(before_facets) & set(after_facets)):
            delta = after_facets[key][0] - before_facets[key][0]
            if delta:
                changes.append(
                    {
                        "kind": "facet_changed",
                        "facet": key[0],
                        "value": key[1],
                        "calls_before": before_facets[key][0],
                        "calls_after": after_facets[key][0],
                        "delta": delta,
                    }
                )
        changes.sort(key=lambda c: (-abs(int(c["delta"])), str(c["kind"])))
        return changes[:cap]

    def _table_present(self, name: str) -> bool:
        if hasattr(self._conn, "table_columns"):
            return bool(self._conn.table_columns(name))
        try:
            self._conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
        except Exception:  # noqa: BLE001 -- optional derived tables may be absent
            return False
        return True

    def _count_profile_calls(self, vids: list[int], sources: set[int]) -> int:
        if not vids:
            return 0
        filt, params = _source_scope_sql(sources if sources else None)
        try:
            row = self._conn.execute(
                "SELECT count(*) FROM call_instances WHERE version_id IN ("
                + ",".join("?" for _ in vids)
                + f") AND in_target_api=1{filt}",
                (*vids, *params),
            ).fetchone()
        except Exception:  # noqa: BLE001 -- health must not fail on odd backends
            return 0
        return int(row[0]) if row else 0

    def _count_profile_scopes(self, vids: list[int], sources: set[int]) -> int:
        if not vids:
            return 0
        filt, params = _source_scope_sql(sources if sources else None)
        scope_expr = "call_instances.scope"
        try:
            row = self._conn.execute(
                f"SELECT count(DISTINCT {scope_expr}) FROM call_instances"
                " WHERE version_id IN ("
                + ",".join("?" for _ in vids)
                + f") AND in_target_api=1{filt}",
                (*vids, *params),
            ).fetchone()
        except Exception:  # noqa: BLE001 -- health must not fail on odd backends
            return 0
        return int(row[0]) if row else 0

    def api_insight(
        self,
        view: str,
        api: str | None = None,
        apis: list[str] | None = None,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Unified API view: frequency ranking, co-occurring APIs, argument shapes."""
        require_limit(limit, high=INSIGHT_ROWS)
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
                tenant_id=tenant_id,
            )
        if view == "usage":
            return self.api_usage_counts(
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 20,
                tenant_id=tenant_id,
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
                tenant_id=tenant_id,
            )
        if view == "args":
            if not api:
                raise ValueError("args view requires api")
            return self._arg_shapes_with_facets(
                api,
                library=library,
                version=version,
                ecosystem=ecosystem,
                limit=limit if limit is not None else 10,
                tenant_id=tenant_id,
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
        tenant_id: str | None = None,
        limit: int | None = None,
        per_file_limit: int | None = None,
        dedup: bool | None = None,
        profile: str | None = None,
        facet: str | None = None,
        compare_profile: str | None = None,
    ) -> list[dict[str, Any]]:
        """Unified pattern view: patterns, evidence, feature tree, clusters, facets."""
        require_limit(limit, high=INSIGHT_ROWS)
        require_limit(per_file_limit, high=PER_FILE_ROWS, name="per_file_limit")
        if view == "health":
            return [
                self.mining_health(
                    library=library,
                    version=version,
                    ecosystem=ecosystem,
                    tenant_id=tenant_id,
                    profile=profile,
                )
            ]
        if view == "labels":
            return self.list_labels(
                library=library,
                version=version,
                ecosystem=ecosystem,
                containing=containing,
                limit=limit if limit is not None else 50,
                tenant_id=tenant_id,
                profile=profile,
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
                tenant_id=tenant_id,
                profile=profile,
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
                tenant_id=tenant_id,
                profile=profile,
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
                tenant_id=tenant_id,
                profile=profile,
            )
        if view == "tree":
            return self.feature_tree(
                library=library,
                version=version,
                ecosystem=ecosystem,
                prefix=prefix,
                limit=limit if limit is not None else 100,
                tenant_id=tenant_id,
                profile=profile,
            )
        if view == "clusters":
            return self.api_clusters(
                library=library,
                version=version,
                ecosystem=ecosystem,
                containing=containing,
                limit=limit if limit is not None else 50,
                tenant_id=tenant_id,
                profile=profile,
            )
        if view == "facets":
            return self.list_facets(
                library=library,
                version=version,
                ecosystem=ecosystem,
                tenant_id=tenant_id,
                facet=facet,
                containing=containing,
                limit=limit if limit is not None else 50,
                profile=profile,
            )
        if view == "feature_summary":
            return self.facet_summary(
                library=library,
                version=version,
                ecosystem=ecosystem,
                tenant_id=tenant_id,
                limit=limit if limit is not None else 50,
                profile=profile,
            )
        if view == "cluster_labels":
            return self.list_cluster_labels(
                library=library,
                version=version,
                ecosystem=ecosystem,
                tenant_id=tenant_id,
                containing=containing,
                limit=limit if limit is not None else 50,
                profile=profile,
            )
        if view == "drift":
            return self.profile_drift(
                library=library,
                version=version,
                ecosystem=ecosystem,
                tenant_id=tenant_id,
                profile=profile,
                compare_profile=compare_profile,
                limit=limit,
            )
        raise ValueError(f"unsupported pattern view: {view}")

    def symbol_insight(
        self,
        view: str,
        api: str | None = None,
        apis: list[str] | None = None,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Thin symbol views over version-scoped entities."""
        require_limit(limit, high=INSIGHT_ROWS)
        if view == "detail":
            wanted = list(apis or []) + ([api] if api else [])
            if not wanted:
                raise ValueError("detail view requires api or apis")
            return self.symbol_details(
                wanted,
                library,
                version,
                ecosystem,
                limit=limit if limit is not None else 100,
                tenant_id=tenant_id,
            )
        if view == "file":
            if not api:
                raise ValueError("file view requires api as file path")
            return self.symbols_in_file(
                api,
                library,
                version,
                ecosystem,
                limit=limit if limit is not None else 200,
                offset=offset,
                tenant_id=tenant_id,
            )
        if view == "children":
            if not api:
                raise ValueError("children view requires api as parent name")
            return self.child_symbols(
                api,
                library,
                version,
                ecosystem,
                limit=limit if limit is not None else 200,
                offset=offset,
                tenant_id=tenant_id,
            )
        if view == "signature":
            if not api:
                raise ValueError("signature view requires api")
            return [
                self.canonical_signature(
                    api, library, version, ecosystem, tenant_id=tenant_id
                )
            ]
        raise ValueError(f"unsupported symbol view: {view}")

    def call_insight(
        self,
        view: str,
        api: str | None = None,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_ambiguous: bool = False,
        resolve_kinds: list[str] | None = None,
        min_args: int | None = None,
        max_args: int | None = None,
        relation_kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Thin bidirectional call views with shared filters."""
        require_limit(limit, high=INSIGHT_ROWS)
        capped = limit if limit is not None else 100
        if view == "callee":
            if not api:
                raise ValueError("callee view requires api as callee name")
            return self.calls_by_callee(
                api,
                library,
                version,
                ecosystem,
                limit=capped,
                offset=offset,
                include_ambiguous=include_ambiguous,
                resolve_kinds=resolve_kinds,
                min_args=min_args,
                max_args=max_args,
                relation_kinds=relation_kinds,
                tenant_id=tenant_id,
            )
        if view == "caller":
            if not api:
                raise ValueError("caller view requires api as caller name")
            return self.calls_by_caller(
                api,
                library,
                version,
                ecosystem,
                limit=capped,
                offset=offset,
                include_ambiguous=include_ambiguous,
                resolve_kinds=resolve_kinds,
                min_args=min_args,
                max_args=max_args,
                relation_kinds=relation_kinds,
                tenant_id=tenant_id,
            )
        if view == "file":
            if not api:
                raise ValueError("file view requires api as file path")
            return self.calls_in_file(
                api,
                library,
                version,
                ecosystem,
                limit=capped,
                offset=offset,
                include_ambiguous=include_ambiguous,
                resolve_kinds=resolve_kinds,
                min_args=min_args,
                max_args=max_args,
                relation_kinds=relation_kinds,
                tenant_id=tenant_id,
            )
        raise ValueError(f"unsupported call view: {view}")

    def import_insight(
        self,
        view: str,
        api: str | None = None,
        *,
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        tenant_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Thin import edge views for fan-in and fan-out."""
        require_limit(limit, high=INSIGHT_ROWS)
        if view == "importers":
            if not api:
                raise ValueError("importers view requires api as target")
            return self.importers_of(
                api,
                library,
                version,
                ecosystem,
                limit=limit if limit is not None else 50,
                offset=offset,
                tenant_id=tenant_id,
            )
        if view == "imports":
            if not api:
                raise ValueError("imports view requires api as file path")
            return self.imports_of_file(
                api,
                library,
                version,
                ecosystem,
                limit=limit if limit is not None else 100,
                offset=offset,
                tenant_id=tenant_id,
            )
        raise ValueError(f"unsupported import view: {view}")
