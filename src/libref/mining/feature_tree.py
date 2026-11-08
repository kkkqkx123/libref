"""分层特征树：按点分前缀聚合调用次数与覆盖作用域数。

纯内存算法，落库由 services 编排。
"""

from __future__ import annotations

__all__ = ["build_feature_tree"]


def build_feature_tree(
    calls: list[tuple[str, str]],
    kinds: dict[str, str] | None = None,
) -> list[dict]:
    """由调用清单构建分层节点。

    每项调用为限定名加作用域标识，作用域标识由共现模块的作用域键给出。
    限定名按点分逐级建节点，种类取实体表映射，缺失时记为包节点。
    频次沿父链累加，调用次数按调用行计，作用域数按标识去重计。
    """
    kinds = kinds or {}
    leaf_calls: dict[str, int] = {}
    leaf_scopes: dict[str, set[str]] = {}
    for name, scope in calls:
        if not name:
            continue
        leaf_calls[name] = leaf_calls.get(name, 0) + 1
        leaf_scopes.setdefault(name, set()).add(scope)
    nodes: dict[str, dict] = {}
    for name in sorted(leaf_calls):
        parts = name.split(".")
        for depth in range(1, len(parts) + 1):
            path = ".".join(parts[:depth])
            slot = nodes.get(path)
            if slot is None:
                parent = ".".join(parts[:depth - 1])
                slot = {
                    "path": path,
                    "parent": parent,
                    "depth": depth,
                    "kind": kinds.get(path, "package") if depth < len(parts) else kinds.get(path, "function"),
                    "calls": 0,
                    "scopes": set(),
                }
                nodes[path] = slot
            slot["calls"] += leaf_calls[name]
            slot["scopes"] |= leaf_scopes[name]
    out = []
    for path in sorted(nodes):
        slot = nodes[path]
        out.append({
            "path": slot["path"],
            "parent": slot["parent"],
            "depth": slot["depth"],
            "kind": slot["kind"],
            "calls": slot["calls"],
            "scopes": len(slot["scopes"]),
        })
    return out
