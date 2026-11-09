"""Scope-level co-occurrence counting, invoked on demand independently of the main mining path."""

from __future__ import annotations

from itertools import combinations


def scope_key(caller_file: str, caller: str | None) -> str:
    """Scope key: file plus enclosing caller, consistent with the retrieval-side grouping convention."""
    return f"{caller_file}\0{caller or ''}"


def build_scope_sets(rows: list[tuple[str, str, str | None]]) -> list[set[str]]:
    """Build scope sets from call rows: each row is a qualified name plus file plus enclosing caller."""
    grouped: dict[str, set[str]] = {}
    for name, caller_file, caller in rows:
        if not name:
            continue
        grouped.setdefault(scope_key(caller_file, caller), set()).add(name)
    return list(grouped.values())


def cooccur_matrix(scope_sets: list[set[str]]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for names in scope_sets:
        for first, second in combinations(sorted(names), 2):
            key = (first, second)
            counts[key] = counts.get(key, 0) + 1
    return counts
