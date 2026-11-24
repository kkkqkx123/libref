"""Candidate disambiguation and filtering."""

from __future__ import annotations

from libref.astminer.resolution.helpers import (
    _SELF_HEADS,
    _normalize_type_name,
    _scope_chain,
)


def _disambiguate(
    candidates: list[str],
    caller: str | None,
    parent_by_qual: dict[str, str | None] | None,
) -> str | None:
    if len(candidates) == 1:
        return candidates[0]
    if caller and parent_by_qual:
        chain = _scope_chain(caller, parent_by_qual)
        scoped = [c for c in candidates if parent_by_qual.get(c) in chain]
        if len(scoped) == 1:
            return scoped[0]
        if scoped:
            candidates = scoped
            if len(candidates) == 1:
                return candidates[0]
    return None


def _filter_candidates(
    candidates: list[str],
    receiver: str | None,
    argument_count: int | None,
    argcounts: dict[str, set[int]] | None,
    alias_map: dict[str, str],
    parent_by_qual: dict[str, str | None] | None,
    raw: str,
) -> list[str]:
    """Disambiguate overloads by arity and reject receiver-bound hijacking."""
    if argument_count is not None and argcounts:
        matched = [
            qual
            for qual in candidates
            if argument_count in argcounts.get(qual, {argument_count})
        ]
        if matched:
            candidates = matched
    if not receiver:
        return candidates
    head = receiver.replace("->", ".").replace("::", ".").partition(".")[0]
    head = head.strip("*& ")
    if head in _SELF_HEADS:
        return candidates
    suffix = "." + raw.partition(".")[2]
    owner = alias_map.get(head, head)
    owner = _normalize_type_name(owner) or head
    return [
        qual
        for qual in candidates
        if qual == owner + suffix
        or qual.startswith(owner + ".")
        or (parent_by_qual or {}).get(qual) == owner
    ]
