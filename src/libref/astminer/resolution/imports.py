"""Import, re-export and alias-chain resolution."""

from __future__ import annotations


def _close_reexports(alias_per_file: dict[str, dict[str, str]]) -> None:
    package_exports: dict[str, str] = {}
    for path, mapping in alias_per_file.items():
        norm = path.replace("\\", "/")
        if norm.endswith("/__init__.py"):
            package = norm[: -len("/__init__.py")].replace("/", ".")
        elif norm == "__init__.py":
            package = ""
        else:
            continue
        for alias, full in list(mapping.items()):
            key = f"{package}.{alias}" if package else alias
            if key not in package_exports:
                package_exports[key] = full
    if not package_exports:
        return
    changed = True
    while changed:
        changed = False
        for mapping in alias_per_file.values():
            for alias, full in list(mapping.items()):
                target = package_exports.get(full)
                if target is not None and target != full:
                    mapping[alias] = target
                    changed = True


def _close_alias_chains(alias_per_file: dict[str, dict[str, str]]) -> None:
    for mapping in alias_per_file.values():
        if len(mapping) < 2:
            continue
        snapshot = dict(mapping)
        for alias, full in snapshot.items():
            if not full:
                continue
            seen = {alias}
            current = full
            for _ in range(len(snapshot) + 1):
                head = current.partition(".")[0] if current else ""
                if not head or head not in snapshot or head in seen:
                    break
                target = snapshot[head]
                if not target:
                    break
                if "." in current:
                    _, _, rest = current.partition(".")
                    current = target + "." + rest if target else current
                else:
                    current = target
                if current.partition(".")[0] == alias:
                    current = full
                    break
                seen.add(head)
            if current != full and current.partition(".")[0] != alias:
                mapping[alias] = current


def _expand_alias(raw: str, alias_map: dict[str, str]) -> str | None:
    if not raw or not alias_map:
        return None
    parts = raw.split(".")
    for length in range(len(parts), 0, -1):
        prefix = ".".join(parts[:length])
        target = alias_map.get(prefix)
        if target:
            if length == len(parts):
                return target
            return target + "." + ".".join(parts[length:])
    return None


def _owner_options(
    name: str,
    declared: set[str],
    suffix_index: dict[str, list[str]],
    same_file: set[str],
) -> list[str]:
    if name in declared:
        return [name]
    options = suffix_index.get(name, [])
    if len(options) == 1:
        return options
    same = [q for q in options if q in same_file]
    if len(same) == 1:
        return same
    return []


def _rewrite_factory_chain(
    resolved: str,
    declared: set[str],
    suffix_index: dict[str, list[str]],
    same_file: set[str],
    return_by_qual: dict[str, str],
) -> str | None:
    current = resolved
    for _ in range(8):
        stepped = _rewrite_factory_head(
            current, declared, suffix_index, same_file, return_by_qual
        )
        if stepped is None or stepped == current:
            break
        current = stepped
    if current == resolved:
        return None
    return current


def _prefer_imported(candidates: list[str], alias_map: dict[str, str]) -> str | None:
    targets = [target for target in alias_map.values() if target]
    if not targets:
        return None
    matched = [
        cand
        for cand in candidates
        if any(cand == target or cand.startswith(target + ".") for target in targets)
    ]
    if len(matched) == 1:
        return matched[0]
    return None


def _rewrite_factory_head(
    resolved: str,
    declared: set[str],
    suffix_index: dict[str, list[str]],
    same_file: set[str],
    return_by_qual: dict[str, str],
) -> str | None:
    if not resolved or "." not in resolved or not return_by_qual:
        return None
    parts = resolved.split(".")
    for length in range(len(parts) - 1, 0, -1):
        func_name = ".".join(parts[:length])
        method_rest = ".".join(parts[length:])
        if not method_rest:
            continue
        funcs = _owner_options(func_name, declared, suffix_index, same_file)
        if len(funcs) != 1:
            continue
        owner_name = (return_by_qual.get(funcs[0]) or "").strip()
        if not owner_name:
            continue
        owners = _owner_options(owner_name, declared, suffix_index, same_file)
        if len(owners) != 1:
            continue
        return owners[0] + "." + method_rest
    return None
