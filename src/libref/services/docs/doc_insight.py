"""Read models over the document-derived tables.

Every view degrades to an empty list when its derived table is absent, and every
list is truncated after filtering so callers see a stable head.
"""

from __future__ import annotations

import json
from typing import Any

VIEWS = ("features", "links", "coverage", "consistency")


class DocInsightService:
    """Version-scoped reads for document facets, alignment, coverage, and verdicts."""

    def __init__(self, conn, settings=None) -> None:
        self._conn = conn
        self._settings = settings

    @staticmethod
    def _table_ready(conn, name: str) -> bool:
        try:
            conn.execute(f"SELECT 1 FROM {name} LIMIT 0").fetchone()
        except Exception:  # noqa: BLE001 — a missing derived table reads as empty
            return False
        return True

    def _version_id(
        self,
        library: str,
        version: str,
        ecosystem: str | None = None,
        tenant_id: str = "default",
    ) -> tuple[int, int]:
        sql = (
            "SELECT v.id, l.id FROM versions v JOIN libraries l ON l.id=v.library_id"
            " WHERE l.name=? AND v.version=?"
        )
        params: list[Any] = [library, version]
        if ecosystem is not None:
            sql += " AND l.ecosystem=?"
            params.append(ecosystem)
        if tenant_id not in (None, "*", "global"):
            try:
                from libref.services.infra.tenant import is_global, table_has_tenant

                if table_has_tenant(self._conn, "versions") and not is_global(
                    str(tenant_id)
                ):
                    sql += " AND v.tenant_id=?"
                    params.append(str(tenant_id))
            except Exception:  # noqa: BLE001, S110
                pass
        row = self._conn.execute(sql, params).fetchone()
        if row is None:
            raise ValueError(f"unknown library version: {library}:{version}")
        return int(row[0]), int(row[1])

    def _profile_id(self, version_id: int, profile: str | None, tenant_id: str) -> int:
        from libref.services.code.usage_sources import resolve_profile

        resolved = resolve_profile(
            self._conn,
            version_id=version_id,
            name=profile,
            tenant_id=tenant_id,
        )
        return int(resolved["id"])

    def insight(
        self,
        library: str,
        version: str,
        *,
        view: str = "features",
        ecosystem: str | None = None,
        profile: str | None = None,
        facet: str | None = None,
        api: str | None = None,
        pattern: str | None = None,
        verdict: str | None = None,
        block_type: str | None = None,
        document_id: int | None = None,
        limit: int = 50,
        tenant_id: str = "default",
    ) -> list[dict[str, Any]]:
        """Dispatch one of the four insight views with a common limit."""
        if view not in VIEWS:
            raise ValueError(f"unsupported doc insight view: {view}")
        if limit <= 0:
            return []
        version_id, library_id = self._version_id(
            library, version, ecosystem, tenant_id
        )
        if view == "features":
            return self.features(
                version_id,
                facet=facet,
                block_type=block_type,
                document_id=document_id,
                limit=limit,
            )
        if view == "links":
            return self.links(version_id, api=api, document_id=document_id, limit=limit)
        if view == "coverage":
            return self.coverage(
                version_id,
                library_id,
                profile=profile,
                tenant_id=tenant_id,
                pattern=pattern,
                document_id=document_id,
                limit=limit,
            )
        return self.consistency(
            version_id,
            library_id,
            profile=profile,
            tenant_id=tenant_id,
            facet=facet,
            verdict=verdict,
            document_id=document_id,
            limit=limit,
        )

    def features(
        self,
        version_id: int,
        *,
        facet: str | None = None,
        block_type: str | None = None,
        document_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not self._table_ready(self._conn, "doc_features"):
            return []
        sql = "SELECT document_id, facet, value FROM doc_features WHERE version_id=?"
        params: list[Any] = [version_id]
        if document_id is not None:
            sql += " AND document_id=?"
            params.append(document_id)
        if facet:
            sql += " AND facet=?"
            params.append(facet)
        sql += " ORDER BY document_id, facet"
        out: list[dict[str, Any]] = []
        for doc_id, name, raw in self._conn.execute(sql, params).fetchall():
            try:
                value = json.loads(raw) if raw else {}
            except TypeError, ValueError:
                value = {}
            if block_type and value.get("kind") != block_type:
                continue
            out.append(
                {
                    "document_id": int(doc_id),
                    "facet": str(name),
                    "kind": value.get("kind"),
                    "details": value.get("details") or {},
                }
            )
            if len(out) >= limit:
                break
        return out

    def links(
        self,
        version_id: int,
        *,
        api: str | None = None,
        document_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not self._table_ready(self._conn, "doc_api_links"):
            return []
        sql = (
            "SELECT l.document_id, l.api_entity_id, ae.qualified_name,"
            " l.match_kind, l.confidence, l.evidence FROM doc_api_links l"
            " JOIN api_entities ae ON ae.id=l.api_entity_id WHERE l.version_id=?"
        )
        params: list[Any] = [version_id]
        if document_id is not None:
            sql += " AND l.document_id=?"
            params.append(document_id)
        if api:
            sql += " AND ae.qualified_name=?"
            params.append(api)
        sql += " ORDER BY l.document_id, ae.qualified_name"
        nodes: dict[tuple[int, int], set[str]] = {}
        if self._table_ready(self._conn, "doc_feature_node_links"):
            node_sql = (
                "SELECT document_id, api_entity_id, node_path"
                " FROM doc_feature_node_links WHERE version_id=?"
            )
            node_params: list[Any] = [version_id]
            if document_id is not None:
                node_sql += " AND document_id=?"
                node_params.append(document_id)
            for doc_id, entity_id, path in self._conn.execute(
                node_sql, node_params
            ).fetchall():
                nodes.setdefault((int(doc_id), int(entity_id)), set()).add(str(path))
        out: list[dict[str, Any]] = []
        for doc_id, entity_id, name, kind, confidence, raw in self._conn.execute(
            sql, params
        ).fetchall():
            try:
                evidence = json.loads(raw) if raw else {}
            except TypeError, ValueError:
                evidence = {}
            out.append(
                {
                    "document_id": int(doc_id),
                    "api": str(name),
                    "match_kind": str(kind),
                    "confidence": float(confidence),
                    "evidence": evidence,
                    "feature_nodes": sorted(
                        nodes.get((int(doc_id), int(entity_id)), set())
                    ),
                }
            )
            if len(out) >= limit:
                break
        return out

    def coverage(
        self,
        version_id: int,
        library_id: int,
        *,
        profile: str | None = None,
        tenant_id: str = "default",
        pattern: str | None = None,
        document_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not self._table_ready(self._conn, "doc_pattern_coverage"):
            return []
        del library_id
        profile_id = self._profile_id(version_id, profile, tenant_id)
        sql = (
            "SELECT document_id, pattern_hash, pattern, covered_kind, support"
            " FROM doc_pattern_coverage WHERE version_id=? AND profile_id=?"
        )
        params: list[Any] = [version_id, profile_id]
        if document_id is not None:
            sql += " AND document_id=?"
            params.append(document_id)
        if pattern:
            sql += " AND pattern_hash=?"
            params.append(pattern)
        sql += " ORDER BY support DESC, document_id, pattern_hash"
        out: list[dict[str, Any]] = []
        for doc_id, digest, raw, kind, support in self._conn.execute(
            sql, params
        ).fetchall():
            try:
                names = json.loads(raw) if raw else []
            except TypeError, ValueError:
                names = []
            out.append(
                {
                    "document_id": int(doc_id),
                    "pattern_hash": str(digest),
                    "pattern": names,
                    "covered_kind": str(kind),
                    "support": int(support),
                }
            )
            if len(out) >= limit:
                break
        return out

    def consistency(
        self,
        version_id: int,
        library_id: int,
        *,
        profile: str | None = None,
        tenant_id: str = "default",
        facet: str | None = None,
        verdict: str | None = None,
        document_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not self._table_ready(self._conn, "doc_consistency"):
            return []
        del library_id
        profile_id = self._profile_id(version_id, profile, tenant_id)
        sql = (
            "SELECT document_id, facet, verdict, note, evidence_hash, model,"
            " prompt_version, updated_at FROM doc_consistency"
            " WHERE version_id=? AND profile_id=?"
        )
        params: list[Any] = [version_id, profile_id]
        if document_id is not None:
            sql += " AND document_id=?"
            params.append(document_id)
        if facet:
            sql += " AND facet=?"
            params.append(facet)
        if verdict:
            sql += " AND verdict=?"
            params.append(verdict)
        sql += " ORDER BY document_id, facet"
        out: list[dict[str, Any]] = []
        for row in self._conn.execute(sql, params).fetchall():
            out.append(
                {
                    "document_id": int(row[0]),
                    "facet": str(row[1]),
                    "verdict": str(row[2]),
                    "note": str(row[3] or ""),
                    "evidence_hash": str(row[4] or ""),
                    "model": str(row[5] or ""),
                    "prompt_version": str(row[6] or ""),
                    "updated_at": str(row[7] or ""),
                }
            )
            if len(out) >= limit:
                break
        return out

    def compare_versions(
        self,
        library: str,
        before: str,
        after: str,
        *,
        ecosystem: str | None = None,
        tenant_id: str = "default",
    ) -> list[dict[str, Any]]:
        """Compare two versions by URL and facet, returning additions and removals."""
        table = "doc_features"
        if not self._table_ready(self._conn, table):
            return []
        before_id, _ = self._version_id(library, before, ecosystem, tenant_id)
        after_id, _ = self._version_id(library, after, ecosystem, tenant_id)
        sql = (
            "SELECT d.url, f.facet, f.value FROM doc_features f"
            " JOIN documents d ON d.id=f.document_id"
            " WHERE f.version_id=? AND f.facet IN ('block_type','deprecated','example_symbols')"
        )

        def load(version_id: int) -> dict[tuple[str, str], Any]:
            out: dict[tuple[str, str], Any] = {}
            for url, facet, raw in self._conn.execute(sql, (version_id,)).fetchall():
                try:
                    value = json.loads(raw) if raw else {}
                except TypeError, ValueError:
                    value = {}
                out[(str(url), str(facet))] = value.get("details", {}).get("value")
            return out

        old = load(before_id)
        new = load(after_id)
        limit = int(getattr(self._settings, "drift_topn", 20) or 20)
        out: list[dict[str, Any]] = []
        for key in sorted(set(new) - set(old)):
            out.append(
                {"url": key[0], "facet": key[1], "kind": "added", "after": new[key]}
            )
            if len(out) >= limit:
                return out
        for key in sorted(set(old) - set(new)):
            out.append(
                {"url": key[0], "facet": key[1], "kind": "removed", "before": old[key]}
            )
            if len(out) >= limit:
                return out
        for key in sorted(set(old) & set(new)):
            if old[key] != new[key]:
                out.append(
                    {
                        "url": key[0],
                        "facet": key[1],
                        "kind": "changed",
                        "before": old[key],
                        "after": new[key],
                    }
                )
                if len(out) >= limit:
                    break
        return out
