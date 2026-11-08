"""频次聚合：精确计数供挖掘截断，层级计数仅供展示。"""

from __future__ import annotations

from collections import Counter


def exact_counts(names: list[str]) -> dict[str, int]:
    return dict(Counter(names))


def level_counts(names: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in names:
        parts = name.split(".")
        for depth in range(1, len(parts) + 1):
            level = ".".join(parts[:depth])
            counts[level] = counts.get(level, 0) + 1
    return counts


def prune_to_top_n(
    counts: dict[str, int], n: int, max_depth: int = 3
) -> dict[str, int]:
    by_depth: dict[int, list[tuple[str, int]]] = {}
    for name, count in counts.items():
        depth = name.count(".") + 1
        if depth > max_depth:
            continue
        by_depth.setdefault(depth, []).append((name, count))
    kept: dict[str, int] = {}
    for entries in by_depth.values():
        entries.sort(key=lambda kv: (-kv[1], kv[0]))
        for name, count in entries[:n]:
            kept[name] = count
    return kept


def top_exact_names(counts: dict[str, int], n: int) -> set[str]:
    """截断唯一入口：按精确计数取前若干限定名，排序稳定可重现。"""
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {name for name, _ in ordered[:n]}
