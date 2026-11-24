"""API frequency, argument shape and co-occurrence reads."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from libref.services.code.mining_read.common import _sibling_filter
from libref.services.registry.libraries import mining_version_ids


class ApiUsageReader:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

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
