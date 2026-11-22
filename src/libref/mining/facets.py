"""Usage facet aggregation and selection: masked shapes, call forms, order buckets, guards.

Pure in-memory algorithms; persistence is orchestrated by services. Each input
row describes one kept call instance; outputs are facet rows ready for bulk
persistence. Guard detection is a syntax-level approximation and stays silent
when the input carries no signal.
"""

from __future__ import annotations

from typing import Any

from libref.mining.idiom import mask_arg

__all__ = [
    "FACET_ARG_SHAPE",
    "FACET_CALL_ORDER",
    "FACET_CALL_SHAPE",
    "FACET_COMBO",
    "FACET_GUARD",
    "aggregate_facets",
    "expand_candidates",
    "facet_callee",
    "mask_call_shape",
    "order_bucket",
    "score_facet_row",
    "select_facets",
    "split_shape_value",
]

FACET_ARG_SHAPE = "arg_shape"
FACET_CALL_SHAPE = "call_shape"
FACET_CALL_ORDER = "call_order"
FACET_COMBO = "arg_shape_call_shape"
FACET_GUARD = "guard"

_CHECK_PREFIXES = ("check", "assert", "verify", "ensure", "validate")

_PAIR_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("open", "close"),
    ("create", "destroy"),
    ("new", "delete"),
    ("new", "free"),
    ("alloc", "free"),
    ("malloc", "free"),
    ("lock", "unlock"),
    ("acquire", "release"),
    ("begin", "end"),
    ("begin", "commit"),
    ("begin", "rollback"),
    ("init", "cleanup"),
    ("start", "stop"),
)


def mask_call_shape(callee: str, args: list[Any] | tuple[Any, ...] | None) -> str:
    """Masked call shape used as the human-readable facet value."""
    masked = ", ".join(mask_arg(str(a)) for a in (args or []))
    return f"{callee}({masked})"


def facet_callee(value: str) -> str:
    """Owning callee of a facet value, independent of the dimension encoding."""
    text = str(value or "")
    cuts = [pos for pos in (text.find("("), text.find("@")) if pos >= 0]
    return text[: min(cuts)] if cuts else text


def split_shape_value(value: str) -> tuple[str, str]:
    """Split an arg-shape value into its callee and masked-shape parts."""
    text = str(value or "")
    head, sep, tail = text.partition("(")
    if not sep:
        return text, ""
    return head, "(" + tail


def order_bucket(rank: int, total: int) -> str:
    """Position bucket of one call inside its ordered sample."""
    if total <= 1:
        return "first"
    ratio = rank / total
    if ratio < 1.0 / 3.0:
        return "first"
    if ratio < 2.0 / 3.0:
        return "middle"
    return "last"


def _short_name(callee: str) -> str:
    return str(callee or "").rsplit(".", 1)[-1].lower()


def _parent_path(callee: str) -> str:
    text = str(callee or "")
    head, _, _ = text.rpartition(".")
    return head


def _guard_pairs(scope_calls: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Paired open/close style usage inside one scope, sharing one owner path."""
    hits: list[tuple[str, str]] = []
    by_parent: dict[str, list[str]] = {}
    for call in scope_calls:
        name = str(call.get("callee") or "")
        if name:
            by_parent.setdefault(_parent_path(name), []).append(name)
    for names in by_parent.values():
        shorts = {name: _short_name(name) for name in names}
        for first, second in _PAIR_SUFFIXES:
            openers = [
                n for n, s in shorts.items() if s == first or s.endswith("_" + first)
            ]
            closers = [
                n for n, s in shorts.items() if s == second or s.endswith("_" + second)
            ]
            for opener in openers:
                for closer in closers:
                    if opener != closer:
                        hits.append((opener, closer))
    return hits


def _guard_checks(scope_calls: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Check-then-use hints: a check-like call earlier in the same scope."""
    hits: list[tuple[str, str]] = []
    ordered = sorted(
        scope_calls, key=lambda c: (int(c.get("line") or 0), int(c.get("order") or 0))
    )
    for pos, call in enumerate(ordered):
        checker = str(call.get("callee") or "")
        if not any(
            _short_name(checker) == stem or _short_name(checker).startswith(stem + "_")
            for stem in _CHECK_PREFIXES
        ):
            continue
        parent = _parent_path(checker)
        for later in ordered[pos + 1 :]:
            target = str(later.get("callee") or "")
            if target and target != checker and _parent_path(target) == parent:
                hits.append((target, checker))
    return hits


def aggregate_facets(
    rows: list[dict[str, Any]],
    *,
    guard_enabled: bool = False,
    candidate_budget: int = 500,
    topn: int = 50,
    min_gain: float = 0.0,
) -> list[dict[str, Any]]:
    """Aggregate kept calls into facet rows with scopes deduplicated.

    Selection (candidate expansion plus contribution filtering plus per-layer
    truncation) runs inside this pipeline; the stored rows are the survivors.
    """
    counts: dict[tuple[str, str], list[Any]] = {}

    def _bump(facet: str, value: str, scope: str) -> None:
        if not value:
            return
        slot = counts.get((facet, value))
        if slot is None:
            slot = [0, set()]
            counts[(facet, value)] = slot
        slot[0] += 1
        if scope:
            slot[1].add(scope)

    by_scope: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        callee = str(row.get("callee") or "")
        if not callee:
            continue
        scope = str(row.get("scope") or "")
        masked = mask_call_shape(callee, row.get("args"))
        _bump(FACET_ARG_SHAPE, masked, scope)
        relation = str(row.get("relation") or "direct")
        _bump(FACET_CALL_SHAPE, f"{callee}@{relation}", scope)
        by_scope.setdefault(scope, []).append(row)
    for scope, calls in by_scope.items():
        ordered = sorted(
            calls, key=lambda c: (int(c.get("line") or 0), int(c.get("order") or 0))
        )
        total = len(ordered)
        for rank, call in enumerate(ordered):
            callee = str(call.get("callee") or "")
            if callee:
                _bump(FACET_CALL_ORDER, f"{callee}@{order_bucket(rank, total)}", scope)
        if guard_enabled and scope:
            for opener, closer in _guard_pairs(ordered):
                _bump(FACET_GUARD, f"{opener}@paired:{closer}", scope)
                _bump(FACET_GUARD, f"{closer}@paired:{opener}", scope)
            for target, checker in _guard_checks(ordered):
                _bump(FACET_GUARD, f"{target}@checked:{checker}", scope)
    base = [
        {"facet": facet, "value": value, "calls": calls, "scopes": len(scopes)}
        for (facet, value), (calls, scopes) in counts.items()
    ]
    combos = expand_candidates(rows, budget=candidate_budget)
    return select_facets(base + combos, topn=topn, min_gain=min_gain)


def expand_candidates(
    rows: list[dict[str, Any]], *, budget: int = 500
) -> list[dict[str, Any]]:
    """Cross masked shapes with call relations per callee, capped by budget."""
    if budget <= 0:
        return []
    grouped: dict[tuple[str, str, str], list[Any]] = {}
    for row in rows:
        callee = str(row.get("callee") or "")
        if not callee:
            continue
        masked = mask_call_shape(callee, row.get("args"))
        relation = str(row.get("relation") or "direct")
        key = (callee, masked, relation)
        slot = grouped.get(key)
        if slot is None:
            slot = [0, set()]
            grouped[key] = slot
        slot[0] += 1
        scope = str(row.get("scope") or "")
        if scope:
            slot[1].add(scope)
    ordered = sorted(
        grouped.items(), key=lambda kv: (-int(kv[1][0]), kv[0][1], kv[0][2])
    )
    out = []
    for (callee, masked, relation), (calls, scopes) in ordered[: max(budget, 0)]:
        out.append(
            {
                "facet": FACET_COMBO,
                "value": f"{masked}@{relation}",
                "calls": int(calls),
                "scopes": len(scopes),
            }
        )
    return out


def select_facets(
    facet_rows: list[dict[str, Any]], *, topn: int = 50, min_gain: float = 0.0
) -> list[dict[str, Any]]:
    """Keep rows with enough contribution share, then truncate per facet."""
    totals: dict[str, int] = {}
    for row in facet_rows:
        facet = str(row.get("facet") or "")
        totals[facet] = totals.get(facet, 0) + int(row.get("calls") or 0)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in facet_rows:
        facet = str(row.get("facet") or "")
        total = totals.get(facet, 0)
        share = (int(row.get("calls") or 0) / total) if total > 0 else 0.0
        if share < min_gain:
            continue
        grouped.setdefault(facet, []).append(row)
    out: list[dict[str, Any]] = []
    for facet in sorted(grouped):
        entries = sorted(
            grouped[facet],
            key=lambda r: (-int(r.get("calls") or 0), str(r.get("value") or "")),
        )
        out.extend(entries[: max(topn, 0)])
    return out


def score_facet_row(
    facet: str,
    value: str,
    calls: int,
    scopes: int,
    *,
    peer_rows: list[dict[str, Any]],
    total_calls: int,
    total_scopes: int,
) -> dict[str, Any]:
    """Read-time quality signals for one facet value: coverage and lift.

    Coverage is the scope share; lift compares the shape rate on its callee
    against the shape rate across callees. Both are computed from stored rows.
    """
    coverage = (scopes / total_scopes) if total_scopes > 0 else 0.0
    concentration = 0.0
    facet_total = sum(int(r.get("calls") or 0) for r in peer_rows)
    if facet_total > 0:
        concentration = calls / facet_total
    lift = 1.0
    if facet == FACET_ARG_SHAPE and total_calls > 0:
        callee, shape = split_shape_value(value)
        callee_total = sum(
            int(r.get("calls") or 0)
            for r in peer_rows
            if facet_callee(str(r.get("value") or "")) == callee
        )
        shape_total = sum(
            int(r.get("calls") or 0)
            for r in peer_rows
            if split_shape_value(str(r.get("value") or ""))[1] == shape and shape
        )
        expected = (callee_total / total_calls) * (shape_total / total_calls)
        observed = calls / total_calls
        if expected > 0:
            lift = observed / expected
    return {
        "facet": facet,
        "value": value,
        "calls": calls,
        "scopes": scopes,
        "coverage": round(coverage, 4),
        "concentration": round(concentration, 4),
        "lift": round(lift, 4),
    }
