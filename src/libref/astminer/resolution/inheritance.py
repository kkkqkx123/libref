"""Receiver, parameter and base-class resolution."""

from __future__ import annotations

from libref.astminer.models import (
    Confidence,
)
from libref.astminer.resolution.helpers import _build_suffix_index, _normalize_type_name
from libref.astminer.resolution.imports import _owner_options


def _resolve_owner_via_bases(
    resolved: str,
    declared: set[str],
    bases_by_qual: dict[str, list[str]],
    suffix_index: dict[str, list[str]],
    same_file: set[str],
) -> str | None:
    if not resolved or "." not in resolved:
        return None
    parts = resolved.split(".")
    for length in range(len(parts) - 1, 0, -1):
        owner_name = ".".join(parts[:length])
        suffix = "." + ".".join(parts[length:])
        owners = _owner_options(owner_name, declared, suffix_index, same_file)
        if len(owners) != 1:
            continue
        owner = owners[0]
        if owner + suffix in declared:
            return owner + suffix
        hit = _resolve_self_via_bases(
            owner, suffix, declared, bases_by_qual, suffix_index, same_file
        )
        if hit is not None:
            return hit
    return None


def _resolve_self_via_bases(
    class_qual: str | None,
    suffix: str,
    declared: set[str],
    bases_by_qual: dict[str, list[str]],
    suffix_index: dict[str, list[str]],
    same_file: set[str],
) -> str | None:
    if not class_qual or not suffix:
        return None
    seen: set[str] = {class_qual}
    frontier: list[str] = [class_qual]
    while frontier:
        next_frontier: list[str] = []
        level_hits: list[str] = []
        for current in frontier:
            for raw_base in bases_by_qual.get(current, []):
                cleaned = _normalize_type_name(raw_base)
                if not cleaned:
                    continue
                options = suffix_index.get(cleaned, [])
                if not options:
                    continue
                if len(options) == 1:
                    base_options = options
                else:
                    same = [q for q in options if q in same_file]
                    if len(same) != 1:
                        continue
                    base_options = same
                for base_qual in base_options:
                    if base_qual in seen:
                        continue
                    seen.add(base_qual)
                    hit = base_qual + suffix
                    if hit in declared and hit not in level_hits:
                        level_hits.append(hit)
                    next_frontier.append(base_qual)
        if len(set(level_hits)) == 1:
            return level_hits[0]
        if level_hits:
            return None
        frontier = next_frontier
    return None


def _shadowed_by_param(
    raw: str,
    caller: str,
    parent_by_qual: dict[str, str | None] | None,
    params_by_qual: dict[str, list[str]],
) -> bool:
    current: str | None = caller
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        if raw in (params_by_qual.get(current) or []):
            return True
        current = parent_by_qual.get(current) if parent_by_qual else None
    return False


def _lookup_param_type(
    name: str,
    caller: str,
    parent_by_qual: dict[str, str | None] | None,
    param_types_by_qual: dict[str, dict[str, str]],
) -> str:
    current: str | None = caller
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        hit = (param_types_by_qual.get(current) or {}).get(name)
        if hit and hit.strip():
            return hit
        current = parent_by_qual.get(current) if parent_by_qual else None
    return ""


def _resolve_via_receiver_type(
    raw: str,
    caller: str,
    parent_by_qual: dict[str, str | None] | None,
    param_types_by_qual: dict[str, dict[str, str]],
    declared: set[str],
    same_file: set[str],
    suffix_index: dict[str, list[str]] | None = None,
    bases_by_qual: dict[str, list[str]] | None = None,
) -> tuple[str, Confidence] | None:
    head, _, rest = raw.partition(".")
    if not head or not rest:
        return None
    annotation = _lookup_param_type(head, caller, parent_by_qual, param_types_by_qual)
    owner = _normalize_type_name(annotation)
    if not owner:
        return None
    resolved = owner + raw[len(head) :]
    if resolved in declared:
        return resolved, "EXTRACTED"
    index = suffix_index if suffix_index is not None else _build_suffix_index(declared)
    suffixed = index.get(resolved, [])
    if len(suffixed) == 1:
        if suffixed[0] in same_file:
            return suffixed[0], "EXTRACTED"
        return suffixed[0], "INFERRED"
    if bases_by_qual:
        inherited = _resolve_owner_via_bases(
            resolved, declared, bases_by_qual, index, same_file
        )
        if inherited is not None:
            return inherited, "EXTRACTED"
    return None
