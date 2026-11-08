"""接口分组：按作用域重合度做层次合并，区分协同与可互换。

纯内存算法，落库由 services 编排。
"""

from __future__ import annotations

__all__ = ["cluster_apis"]


def cluster_apis(
    scope_sets: list[set[str]],
    *,
    threshold: float = 0.15,
    min_size: int = 2,
) -> list[dict]:
    """由作用域集合分组接口，重合度达阈值即连边，连通分量为一组。

    重合度为交集作用域数除以并集作用域数，排序与合并全程确定性。
    每组记成员列表、代表成员与规模，代表取出现作用域多者，相同取名称排序首位。
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
