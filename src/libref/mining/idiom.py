"""Readable idiom assembly, evidence deduplication, and reuse health signals.

Blueprint ref/codescholar (MIT): span-based snippet还原, multi-API
intersection pre-screening, seed-program clustering for dedup, and
reusability/diversity convergence metrics. Only the low-cost engineering
ideas are borrowed here; no graph neural network, no extra middleware.
All helpers are pure in-memory and never touch the database.
"""

from __future__ import annotations

import math
import re
from typing import Any

__all__ = [
    "deduplicate_evidence",
    "mask_arg",
    "neighbour_keep",
    "normalize_signature",
    "reuse_diversity",
    "synthesize_snippet",
]

_CONF_RANK = {"EXTRACTED": 0, "INFERRED": 1, "AMBIGUOUS": 2}

_STR_RE = re.compile(r"""^(['\"]).*?\1$""")
_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def mask_arg(text: str) -> str:
    """Mask a single argument summary to a placeholder while keeping keyword shape."""
    raw = (text or "").strip()
    if not raw:
        return "_"
    if "=" in raw and not raw.startswith(("=", "==")):
        key, _, val = raw.partition("=")
        key = key.strip()
        val = val.strip()
        if not key:
            return "_"
        if not val:
            return f"{key}=_"
        if _STR_RE.match(val):
            return f"{key}='...'"
        if _NUM_RE.match(val):
            return f"{key}=0"
        if val in ("True", "False", "None"):
            return f"{key}={val}"
        return f"{key}=_"
    if _STR_RE.match(raw):
        return "'...'"
    if _NUM_RE.match(raw):
        return "0"
    if raw in ("True", "False", "None"):
        return raw
    return "_"


def _masked_call(callee: str, args: list[str]) -> str:
    masked = ", ".join(mask_arg(a) for a in (args or []))
    return f"{callee}({masked})"


def neighbour_keep(matched: list[int], total: int) -> list[int]:
    """Ordered indices to retain: matched calls plus one neighbour on each side."""
    if not matched or total <= 0:
        return []
    ordered = sorted(set(matched))
    keep: set[int] = set(ordered)
    if ordered[0] - 1 >= 0:
        keep.add(ordered[0] - 1)
    if ordered[-1] + 1 < total:
        keep.add(ordered[-1] + 1)
    return sorted(i for i in keep if 0 <= i < total)


def synthesize_snippet(
    pattern: list[str],
    calls: list[dict[str, Any]],
    matched: list[int],
) -> dict[str, Any]:
    """Assemble a readable snippet from matched calls with neighbours and holes."""
    if not pattern or not calls or not matched:
        return {"lines": [], "skipped": 0, "invalid": True}
    ordered = neighbour_keep(list(matched), len(calls))
    if not ordered:
        return {"lines": [], "skipped": 0, "invalid": True}
    hit = set(matched)
    lines: list[dict[str, Any]] = []
    skipped = 0
    prev: int | None = None
    for idx in ordered:
        if prev is not None and idx - prev > 1:
            skipped += idx - prev - 1
            gap_calls = calls[prev + 1 : idx]
            gap_lines = [c for c in gap_calls if c.get("line_start") is not None]
            if gap_lines:
                span = (
                    f"{gap_lines[0].get('line_start')}-{gap_lines[-1].get('line_end')}"
                )
            else:
                span = f"{prev + 1}-{idx - 1}"
            lines.append({"gap": True, "skipped": idx - prev - 1, "span": span})
        call = calls[idx]
        args = call.get("args") or []
        str_args = [str(a) for a in args]
        entry: dict[str, Any] = {
            "gap": False,
            "callee": str(call.get("callee") or ""),
            "text": _masked_call(str(call.get("callee") or ""), str_args),
            "lines": f"{call.get('line_start')}-{call.get('line_end')}",
            "confidence": str(call.get("confidence") or ""),
            "hit": idx in hit,
        }
        sig = str(call.get("callee_signature") or "").strip()
        if sig:
            entry["signature"] = sig
        resolve = str(call.get("resolve_kind") or "").strip()
        if resolve:
            entry["resolve_kind"] = resolve
        lines.append(entry)
        prev = idx
    return {"lines": lines, "skipped": skipped, "invalid": False}


def normalize_signature(calls: list[dict[str, Any]], matched: list[int]) -> str:
    """Normalized snippet signature used for cross-file dedup."""
    parts: list[str] = []
    for idx in sorted(set(matched)):
        if 0 <= idx < len(calls):
            call = calls[idx]
            args = call.get("args") or []
            masked = ",".join(mask_arg(str(a)) for a in args)
            parts.append(f"{call.get('callee')}({masked})")
    return "|".join(parts)


def _confidence_rank(name: str | None) -> int:
    return _CONF_RANK.get(str(name or ""), 3)


def deduplicate_evidence(
    groups: list[dict[str, Any]],
    *,
    per_file_limit: int = 1,
    cross_file_dedup: bool = True,
) -> list[dict[str, Any]]:
    """Diversify evidence: at most one representative per file, then merge identical signatures."""
    per_file_limit = max(per_file_limit, 1)
    by_file: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        by_file.setdefault(str(group.get("caller_file") or ""), []).append(group)

    def _rank(group: dict[str, Any]) -> tuple[int, int, str, str]:
        matched = group.get("matched") or []
        calls = group.get("calls") or []
        worst = max(
            (_confidence_rank(c.get("confidence")) for c in calls),
            default=3,
        )
        return (
            -len(matched),
            worst,
            str(group.get("caller_file") or ""),
            str(group.get("caller") or ""),
        )

    picked: list[dict[str, Any]] = []
    for path in sorted(by_file):
        ranked = sorted(by_file[path], key=_rank)
        picked.extend(ranked[:per_file_limit])
    picked.sort(
        key=lambda g: (
            -len(g.get("matched") or []),
            str(g.get("caller_file") or ""),
            str(g.get("caller") or ""),
        )
    )
    if not cross_file_dedup:
        return picked
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for group in picked:
        sig = normalize_signature(group.get("calls") or [], group.get("matched") or [])
        if sig and sig in seen:
            continue
        if sig:
            seen.add(sig)
        out.append(group)
    return out


def reuse_diversity(
    patterns: list[dict[str, Any]], clusters: list[dict[str, Any]]
) -> dict[str, Any]:
    """Health signals computed from fetched rows: average reuse and coverage diversity."""
    total = sum(int(p.get("support", 0) or 0) for p in patterns)
    count = len(patterns)
    reuse = math.log(total / count) if count >= 2 and total > count else 0.0
    diversity = math.log(len(clusters)) if clusters else 0.0
    return {
        "patterns": count,
        "total_support": total,
        "reusability": reuse,
        "diversity": diversity,
        "clusters": len(clusters),
    }
