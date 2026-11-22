"""Evidence assembly and display formatting for the mining read model."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from libref.text.snippet import call_tag, gap_tag, unit_tag


def _load_target_version(raw: Any) -> str:
    if not raw:
        return ""
    try:
        value = json.loads(str(raw))
    except ValueError:
        return ""
    if not isinstance(value, dict):
        return ""
    return str(value.get("declared_library_version") or "")


def source_labels(conn, source_ids: set[int]) -> dict[int, dict[str, Any]]:
    """Render source role and identity for evidence without exposing secrets."""
    if not source_ids:
        return {}
    if hasattr(conn, "table_columns") and not conn.table_columns("code_sources"):
        return {}
    marks = ",".join("?" for _ in source_ids)
    try:
        rows = conn.execute(
            "SELECT id, source_key, source_role, origin_kind, source_url, selection,"
            " commit_sha, target_filter"
            f" FROM code_sources WHERE id IN ({marks})",
            tuple(sorted(source_ids)),
        ).fetchall()
    except Exception:  # noqa: BLE001 -- source metadata is advisory for evidence
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            selection = json.loads(str(row[5])) if row[5] else {}
        except ValueError:
            selection = {}
        out[int(row[0])] = {
            "source_key": str(row[1]),
            "source_role": str(row[2]),
            "origin_kind": str(row[3]),
            "source_url": str(row[4] or ""),
            "heading": str((selection or {}).get("heading") or ""),
            "commit_sha": str(row[6] or ""),
            "declared_target_version": str(_load_target_version(row[7])),
        }
    return out


def canonical_url(
    source_url: str, commit_sha: str, path: str, line_start: int, line_end: int
) -> str:
    """Build a commit-pinned URL for GitHub-style repository sources."""
    if not source_url.startswith(("http://", "https://")) or not commit_sha:
        return ""
    parts = urlsplit(source_url)
    segments = [part for part in parts.path.split("/") if part]
    if len(segments) < 2:
        return ""
    segments[1] = segments[1].removesuffix(".git")
    rel = str(path or "").strip().lstrip("./")
    if not rel:
        return ""
    suffix = f"#L{int(line_start)}-L{int(line_end)}" if line_start else ""
    return (
        f"{parts.scheme}://{parts.netloc}/{'/'.join(segments[:2])}/blob/"
        f"{commit_sha}/{rel}{suffix}"
    )


def subsequence_positions(pattern: list[str], names: list[str]) -> list[int]:
    """Locate the first ordered occurrence of pattern inside names."""
    pos: list[int] = []
    cursor = 0
    for want in pattern:
        while cursor < len(names) and names[cursor] != want:
            cursor += 1
        if cursor >= len(names):
            return []
        pos.append(cursor)
        cursor += 1
    return pos


def render_evidence(
    group: dict[str, Any], calls: list[dict[str, Any]], matched: list[int]
) -> str:
    """Render matched calls with neighbours and explicit gaps, body verbatim."""
    from libref.mining.idiom import neighbour_keep

    ordered = neighbour_keep(list(matched), len(calls))
    parts: list[str] = []
    prev: int | None = None
    for idx in ordered:
        if prev is not None and idx - prev > 1:
            skipped = calls[prev + 1 : idx]
            lines = [c for c in skipped if c.get("line_start") is not None]
            if lines:
                span = f"{lines[0].get('line_start')}-{lines[-1].get('line_end')}"
            else:
                span = f"{prev + 1}-{idx - 1}"
            parts.append(gap_tag(len(skipped), span))
        call = calls[idx]
        args = call.get("args") or []
        body = ", ".join(str(part) for part in args)
        attrs: dict[str, str] = {
            "callee": str(call.get("callee") or ""),
            "lines": f"{call.get('line_start')}-{call.get('line_end')}",
            "confidence": str(call.get("confidence") or ""),
            "resolve": str(call.get("resolve_kind") or ""),
            "target": str(call.get("target_status") or ""),
            "relation": str(call.get("relation_kind") or "direct"),
            "hit": "true" if idx in set(matched) else "false",
        }
        if call.get("relation_kind") == "macro":
            attrs["macro_body"] = "true"
            attrs["macro_host"] = str(call.get("macro_host") or "")
            attrs["macro_location"] = str(call.get("definition_location") or "")
            if call.get("definition_url"):
                attrs["definition_url"] = str(call.get("definition_url") or "")
        sig = str(call.get("callee_signature") or "").strip()
        if sig:
            attrs["signature"] = sig
        parts.append(call_tag(attrs, body))
        prev = idx
    inner = "\n".join(parts)
    head = {
        "file": str(group.get("caller_file") or ""),
        "caller": str(group.get("caller") or ""),
        "library": str(group.get("library") or ""),
        "version": str(group.get("version") or ""),
        "source": str(group.get("source_role") or ""),
        "source_key": str(group.get("source_key") or ""),
        "sample": str(group.get("sample_id") or ""),
        "profile": str(group.get("profile") or ""),
        "profile_fingerprint": str(group.get("profile_fingerprint") or ""),
        "canonical_url": str(group.get("canonical_url") or ""),
        "source_hint": str(group.get("source_hint") or ""),
        "macro_policy": str(group.get("macro_policy") or "exclude"),
    }
    if any(call.get("relation_kind") == "macro" for call in calls):
        head["macro_note"] = (
            "macro-body visible call at its definition point, not a preprocessed call site"
        )
    return unit_tag(head, inner)


_KIND_PRIORITY = {
    "class": 0,
    "interface": 1,
    "enum": 2,
    "struct": 3,
    "record": 4,
    "constructor": 5,
    "method": 6,
    "function": 7,
    "package": 8,
    "module": 9,
}


def kind_rank(kind: str | None) -> int:
    """Deterministic order across overloaded qualified names."""
    return _KIND_PRIORITY.get(str(kind or ""), 99)


def choose_canonical(
    rows: list[tuple[str, str, list, str]],
) -> tuple[str, str, list, str]:
    """Pick one representative across kinds, preferring real signatures."""
    ordered = sorted(
        rows,
        key=lambda r: (
            0 if str(r[1] or "").strip() else 1,
            kind_rank(r[0]),
            str(r[0] or ""),
        ),
    )
    return ordered[0]
