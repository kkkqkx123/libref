"""Label, tree, cluster, facet and drift reads."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from libref.services.code.mining_read.common import (
    ProfileContext,
    _profile_context,
    _source_scope_sql,
)
from libref.services.registry.libraries import mining_version_ids


class FeatureReader:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

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
