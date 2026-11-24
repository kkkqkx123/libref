"""Shared profile context and table probes for mining read models."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from libref.services.code.usage_sources import profile_source_context

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


class ReadSupport:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

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

    def _table_present(self, name: str) -> bool:
        if hasattr(self._conn, "table_columns"):
            return bool(self._conn.table_columns(name))
        try:
            self._conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
        except Exception:  # noqa: BLE001 -- optional derived tables may be absent
            return False
        return True
