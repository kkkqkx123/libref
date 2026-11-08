"""文件级共现计数，独立于挖掘主路按需调用。"""

from __future__ import annotations

from itertools import combinations


def cooccur_matrix(file_sets: list[set[str]]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for names in file_sets:
        for first, second in combinations(sorted(names), 2):
            key = (first, second)
            counts[key] = counts.get(key, 0) + 1
    return counts
