"""Mining distribution, quality and health reads."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from libref.astminer.models import TARGET_STATUS_PREFIX
from libref.limits import INSIGHT_ROWS
from libref.services.code.mining_read.common import (
    ProfileContext,
    _profile_context,
    _source_scope_sql,
)
from libref.services.registry.libraries import mining_version_ids


class HealthReader:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

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
