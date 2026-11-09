"""API grouping: hierarchical merging by scope overlap, distinguishing co-use from interchangeability.

Pure in-memory algorithm; persistence is orchestrated by services.
"""

from __future__ import annotations

__all__ = ["cluster_apis"]


def cluster_apis(
    scope_sets: list[set[str]],
    *,
    threshold: float = 0.15,
    min_size: int = 2,
) -> list[dict]:
    """Group APIs from scope sets; connect an edge when overlap reaches the threshold, each connected component becomes a group.

    Overlap is the number of intersecting scopes divided by the union scope count; sorting and merging are fully deterministic.
    Each group records its member list, representative member, and size; the representative is the member with the most scopes, ties broken by name order.
    """
    occ: dict[str, int] = {}
    pair: dict[tuple[str, str], int] = {}
    for names in scope_sets:
        cleaned = sorted({n for n in names if n})
        for name in cleaned:
            occ[name] = occ.get(name, 0) + 1
        for i in range(len(cleaned)):
            for j in range(i + 1, len(cleaned)):
                key = (cleaned[i], cleaned[j])
                pair[key] = pair.get(key, 0) + 1
    parent: dict[str, str] = {name: name for name in occ}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if ra > rb:
            ra, rb = rb, ra
        parent[rb] = ra

    for (a, b), inter in sorted(pair.items()):
        union_size = occ[a] + occ[b] - inter
        score = inter / union_size if union_size else 0.0
        if score >= threshold:
            union(a, b)
    groups: dict[str, list[str]] = {}
    for name in occ:
        groups.setdefault(find(name), []).append(name)
    out = []
    for members in groups.values():
        if len(members) < min_size:
            continue
        members = sorted(members)
        representative = min(members, key=lambda n: (-occ[n], n))
        out.append({
            "members": members,
            "representative": representative,
            "size": len(members),
        })
    out.sort(key=lambda g: (-g["size"], g["members"]))
    for index, group in enumerate(out):
        group["cluster_id"] = index
    return out
