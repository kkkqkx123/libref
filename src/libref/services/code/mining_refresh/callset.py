"""Shared call-instance selection for mining refreshes."""

from __future__ import annotations

from typing import Any

from libref.astminer.models import (
    TARGET_STATUS_PREFIX,
    TARGET_STATUS_RESOLVED,
    TARGET_STATUS_UNCONFIRMED,
)


def _review_map(conn, version_id: int) -> dict[int, str]:
    from libref.services.ops.review import ReviewService

    return ReviewService(conn).review_map_for_version(version_id)


def _keep_call(
    confidence: str,
    resolve_kind: str,
    status: str | None,
    include_ambiguous: bool,
    relation_kind: str = "direct",
    relation_kinds: set[str] | None = None,
    target_status: str = TARGET_STATUS_RESOLVED,
    include_unconfirmed: bool = False,
    macro_policy: str = "exclude",
) -> bool:
    if status == "conflict":
        return False
    if relation_kind == "macro":
        if macro_policy == "review_only":
            return True
        if macro_policy == "approved_only":
            return status == "approved"
        return False
    if (
        target_status == TARGET_STATUS_UNCONFIRMED
        and not include_unconfirmed
        and status != "approved"
    ):
        return False
    if target_status == TARGET_STATUS_PREFIX and not include_ambiguous:
        return status == "approved"
    if status == "approved":
        return True
    if relation_kinds and relation_kind not in relation_kinds:
        return False
    # Stdlib/builtin rows are certain externals (INFERRED), so the mining
    # exclusion rides on the source column instead of the confidence level.
    if confidence == "AMBIGUOUS" or resolve_kind in ("stdlib", "builtin"):
        return bool(include_ambiguous)
    return True


def _load_kept_calls(
    conn,
    version_id: int,
    *,
    include_ambiguous: bool = False,
    include_unconfirmed: bool = False,
    resolve_kinds: list[str] | tuple[str, ...] | None = None,
    min_args: int | None = None,
    max_args: int | None = None,
    source_ids: list[int] | tuple[int, ...] | None = None,
    relation_kinds: list[str] | tuple[str, ...] | None = None,
    macro_policy: str = "exclude",
):
    """Single shared scan with review, target and source-aware filters."""
    if source_ids is not None and not source_ids:
        return []
    wanted = sorted({str(k) for k in (resolve_kinds or []) if str(k).strip()})
    wanted_relations = {str(k) for k in (relation_kinds or []) if str(k).strip()}
    if macro_policy in ("review_only", "approved_only") and relation_kinds is not None:
        wanted_relations.add("macro")
    sql = (
        "SELECT callee_name, caller_file, caller, confidence, id,"
        " line_start, call_order, start_byte, resolve_kind, argument_count, sample_id,"
        " COALESCE(relation_kind,'direct'), COALESCE(target_status,'resolved_target'),"
        " args"
        " FROM call_instances WHERE version_id=? AND in_target_api=1"
    )
    params: list[Any] = [version_id]
    if source_ids is not None:
        marks = ",".join("?" for _ in source_ids)
        sql += f" AND source_id IN ({marks})"
        params.extend(int(v) for v in source_ids)
    if wanted:
        marks = ",".join("?" for _ in wanted)
        sql += f" AND COALESCE(resolve_kind,'') IN ({marks})"
        params.extend(wanted)
    if wanted_relations:
        marks = ",".join("?" for _ in wanted_relations)
        sql += f" AND COALESCE(relation_kind,'direct') IN ({marks})"
        params.extend(sorted(wanted_relations))
    if min_args is not None:
        sql += " AND COALESCE(argument_count,0) >= ?"
        params.append(int(min_args))
    if max_args is not None:
        sql += " AND COALESCE(argument_count,0) <= ?"
        params.append(int(max_args))
    rows = conn.execute(sql, params).fetchall()
    review = _review_map(conn, version_id)
    macro_names: set[str] = set()
    if macro_policy == "exclude":
        macro_names = {
            str(row[0])
            for row in conn.execute(
                "SELECT qualified_name FROM api_entities"
                " WHERE version_id=? AND kind='macro'",
                (version_id,),
            ).fetchall()
        }
    out = []
    for r in rows:
        if macro_policy == "exclude" and str(r[0]) in macro_names:
            continue
        if _keep_call(
            str(r[3]),
            str(r[8] or ""),
            review.get(int(r[4])),
            include_ambiguous,
            str(r[11] or "direct"),
            wanted_relations,
            str(r[12] or TARGET_STATUS_RESOLVED),
            include_unconfirmed,
            macro_policy,
        ):
            out.append(r)
    return out
