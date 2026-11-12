"""Hierarchical feature tree: aggregates call counts and covered scope counts by ownership chain.

Pure in-memory algorithm; persistence is orchestrated by services.
"""

from __future__ import annotations

__all__ = ["build_feature_tree"]


def build_feature_tree(
    calls: list[tuple[str, str]],
    kinds: dict[str, str] | None = None,
    parents: dict[str, str | None] | None = None,
) -> list[dict]:
    """Build hierarchical nodes from a call list.

    Each call is a qualified name plus a scope key, where the scope key is given by the co-occurrence module's scope_key.
    True ownership from the entity table is preferred; dot splitting is only a fallback for names without ownership.
    Frequencies accumulate up the parent chain: call counts are per call row, scope counts are deduplicated by key.
    """
    kinds = kinds or {}
    ownership = parents or {}
    leaf_calls: dict[str, int] = {}
    leaf_scopes: dict[str, set[str]] = {}
    for name, scope in calls:
        if not name:
            continue
        leaf_calls[name] = leaf_calls.get(name, 0) + 1
        leaf_scopes.setdefault(name, set()).add(scope)
    nodes: dict[str, dict] = {}
    for name in sorted(leaf_calls):
        chain = _ownership_chain(name, ownership)
        if chain is None:
            parts = name.split(".")
            chain = [".".join(parts[:depth]) for depth in range(1, len(parts) + 1)]
        for depth, path in enumerate(chain, start=1):
            slot = nodes.get(path)
            if slot is None:
                parent = chain[depth - 2] if depth >= 2 else ""
                slot = {
                    "path": path,
                    "parent": parent,
                    "depth": depth,
                    "kind": kinds.get(path, "package")
                    if depth < len(chain)
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


def _ownership_chain(name: str, ownership: dict[str, str | None]) -> list[str] | None:
    current = ownership.get(name)
    if not current:
        return None
    chain: list[str] = [name]
    seen: set[str] = {name}
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        current = ownership.get(current)
    chain.reverse()
    cleaned = [c for c in chain if c]
    if not cleaned or cleaned[-1] != name:
        return None
    return cleaned
