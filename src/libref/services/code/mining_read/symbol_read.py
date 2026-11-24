"""Symbol, import and call fact reads."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from libref.services.code.mining_evidence import (
    kind_rank,
)
from libref.services.code.mining_read.common import _call_filter_sql, _parse_json_list
from libref.services.registry.libraries import mining_version_ids


class SymbolReader:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

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
