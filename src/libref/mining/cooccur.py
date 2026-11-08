"""作用域级共现计数，独立于挖掘主路按需调用。"""

from __future__ import annotations

from itertools import combinations


def scope_key(caller_file: str, caller: str | None) -> str:
    """作用域标识：文件加外围调用者，与检索侧分组口径一致。"""
    return f"{caller_file}\0{caller or ''}"


def build_scope_sets(rows: list[tuple[str, str, str | None]]) -> list[set[str]]:
    """由调用行构造作用域集合：每行限定名加文件加外围调用者。"""
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
