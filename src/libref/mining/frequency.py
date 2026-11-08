"""层级频次聚合与可选剪枝，层级按限定名点分段定义。"""

from __future__ import annotations


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
