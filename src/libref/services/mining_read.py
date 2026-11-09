"""Mining read model: call frequency, co-occurrence, patterns, trees, clusters."""

from __future__ import annotations

import json
from typing import Any

from libref.services.libraries import mining_version_ids


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
        conf = "" if include_ambiguous else "AND confidence != 'AMBIGUOUS'"
        sql = (
            "SELECT callee_name, count(*), "
            "count(DISTINCT caller_file || char(0) || COALESCE(caller, '')) "
            "FROM call_instances "
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
        conf = "" if include_ambiguous else "AND confidence != 'AMBIGUOUS'"
        rows = self._conn.execute(
            "SELECT args, count(*) FROM call_instances "
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
        conf = "" if include_ambiguous else "AND c.confidence != 'AMBIGUOUS'"
        tconf = "" if include_ambiguous else "AND confidence != 'AMBIGUOUS'"
        rows = self._conn.execute(
            "WITH target(file, caller_key) AS ("
            " SELECT DISTINCT caller_file, COALESCE(caller, '')"
            " FROM call_instances"
            f" WHERE version_id IN ({placeholders}) AND callee_name = ? {tconf})"
            " SELECT c.callee_name,"
            " count(DISTINCT c.caller_file || char(0) || COALESCE(c.caller, '')),"
            " count(*) FROM call_instances c"
            " JOIN target t ON c.caller_file = t.file"
            " AND COALESCE(c.caller, '') = t.caller_key"
            f" WHERE c.version_id IN ({placeholders}) AND c.callee_name != ? {conf}"
            " GROUP BY c.callee_name"
            " ORDER BY 2 DESC, 3 DESC, c.callee_name LIMIT ?",
            (*vids, api, *vids, api, limit),
        ).fetchall()
        return [
            {"api": r[0], "scopes": r[1], "calls": r[2]} for r in rows
        ]

    def list_patterns(
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
            "SELECT pattern, support FROM seq_patterns"
            f" WHERE version_id IN ({placeholders})"
            " ORDER BY support DESC LIMIT ?",
            (*vids, limit),
        ).fetchall()
        out = []
        for raw, support in rows:
            try:
                names = json.loads(raw)
            except ValueError:
                continue
            if containing and not all(n in names for n in containing):
                continue
            out.append({"pattern": names, "support": support})
        return out

    def pattern_evidence(
        self,
        pattern: list[str],
        library: str | None = None,
        version: str | None = None,
        ecosystem: str | None = None,
        limit: int = 5,
        include_ambiguous: bool = False,
    ) -> list[dict[str, Any]]:
        if not pattern:
            return []
        vids = mining_version_ids(self._conn, library, version, ecosystem)
        if not vids:
            return []
        placeholders = ",".join("?" for _ in vids)
        conf = "" if include_ambiguous else "AND ci.confidence != 'AMBIGUOUS'"
        rows = self._conn.execute(
            "SELECT ci.version_id, l.name, v.version, ci.caller_file, ci.caller,"
            " ci.callee_name, ci.args, ci.line_start, ci.line_end, ci.confidence,"
            " ci.call_order, ci.start_byte, ci.id"
            " FROM call_instances ci"
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
            slot["calls"].append({
                "callee": callee,
                "args": parsed,
                "line_start": ls,
                "line_end": le,
                "confidence": cf,
            })
        out = []
        for key in order:
            names = [c["callee"] for c in groups[key]["calls"]]
            if is_subsequence(pattern, names):
                out.append(groups[key])
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
            params.extend([prefix, prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%"])
        params.append(limit)
        rows = self._conn.execute(
            "SELECT path, parent, depth, kind, calls, scopes FROM feature_nodes"
            f" WHERE version_id IN ({placeholders}){filt}"
            " ORDER BY calls DESC, path LIMIT ?",
            params,
        ).fetchall()
        return [
            {"path": r[0], "parent": r[1], "depth": r[2], "kind": r[3], "calls": r[4], "scopes": r[5]}
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
                slot = {"cluster_id": int(cid), "members": [], "size": int(size), "representative": rep}
                grouped[int(cid)] = slot
                order.append(int(cid))
            slot["members"].append(member)
        out = [grouped[cid] for cid in order]
        if containing:
            out = [g for g in out if all(n in g["members"] for n in containing)]
        return out[:limit]
