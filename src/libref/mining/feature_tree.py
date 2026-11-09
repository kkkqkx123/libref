"""Hierarchical feature tree: aggregates call counts and covered scope counts by dot-separated prefix.

Pure in-memory algorithm; persistence is orchestrated by services.
"""

from __future__ import annotations

__all__ = ["build_feature_tree"]


def build_feature_tree(
    calls: list[tuple[str, str]],
    kinds: dict[str, str] | None = None,
) -> list[dict]:
    """Build hierarchical nodes from a call list.

    Each call is a qualified name plus a scope key, where the scope key is given by the co-occurrence module's scope_key.
    Qualified names become nodes level by level on dot boundaries; kinds come from the entity table mapping, defaulting to package nodes when absent.
    Frequencies accumulate up the parent chain: call counts are per call row, scope counts are deduplicated by key.
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
                parent = ".".join(parts[: depth - 1])
                slot = {
                    "path": path,
                    "parent": parent,
                    "depth": depth,
                    "kind": kinds.get(path, "package")
                    if depth < len(parts)
                    else kinds.get(path, "function"),
                    "calls": 0,
                    "scopes": set(),
                }
                nodes[path] = slot
            slot["calls"] += leaf_calls[name]
            slot["scopes"] |= leaf_scopes[name]
    out = []
    for path in sorted(nodes):
        slot = nodes[path]
        out.append(
            {
                "path": slot["path"],
                "parent": slot["parent"],
                "depth": slot["depth"],
                "kind": slot["kind"],
                "calls": slot["calls"],
                "scopes": len(slot["scopes"]),
            }
        )
    return out
