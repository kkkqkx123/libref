"""Shared resolution facts and type/name helpers."""

from __future__ import annotations

from libref.astminer import stdlib as _stdlib

_SELF_HEADS = frozenset({"self", "cls", "this", "Self"})


def _strip_glued_new(
    raw: str, alias_map: dict[str, str], index: dict[str, list[str]]
) -> str:
    """Undo the whitespace-strip gluing of `new X(...)` chain heads.

    The extraction fallback removes all whitespace from composite receiver
    text, so `new commander.Command(...).help` arrives with head
    `newcommander`. When the remainder after `new` is a known head (alias,
    declared suffix, or stdlib root), restore it so lookup succeeds. Real
    identifiers that merely start with "new" stay untouched.
    """
    head, sep, tail = raw.partition(".")
    if not sep or not head.startswith("new") or len(head) <= 3:
        return raw
    remainder = head[3:]
    first = remainder[0]
    if not (first.isalpha() or first == "_"):
        return raw
    if remainder in alias_map or remainder in index or _is_stdlib_target(remainder):
        return f"{remainder}.{tail}" if tail else remainder
    return raw


def _language_for_file(path: str | None) -> str | None:
    if not path:
        return None
    from pathlib import Path as _Path

    from libref.astminer import config as _config

    cfg = _config.config_for_suffix(_Path(path).suffix)
    return cfg.name if cfg is not None else None


def _build_suffix_index(declared: set[str]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for qual in declared:
        slot = index.setdefault(qual, [])
        if qual not in slot:
            slot.append(qual)
        parts = qual.split(".")
        for start in range(1, len(parts)):
            suffix = ".".join(parts[start:])
            if not suffix:
                continue
            bucket = index.setdefault(suffix, [])
            if qual not in bucket:
                bucket.append(qual)
    for key, value in index.items():
        index[key] = sorted(value)
    return index


def _ancestor_list(
    caller: str | None,
    parent_by_qual: dict[str, str | None] | None,
) -> list[str]:
    out: list[str] = []
    current = caller
    seen: set[str] = set()
    while current is not None and current not in seen:
        seen.add(current)
        out.append(current)
        current = parent_by_qual.get(current) if parent_by_qual else None
    return out


def _scope_chain(caller: str, parent_by_qual: dict[str, str | None]) -> set[str | None]:
    chain: set[str | None] = set()
    current: str | None = caller
    while current is not None:
        chain.add(current)
        current = parent_by_qual.get(current)
    chain.add(None)
    return chain


def _normalize_type_name(raw_type: str) -> str:
    text = raw_type.strip().strip("\"'")
    if not text:
        return ""
    text = text.split("|")[0].strip()
    text = text.removeprefix("...").removesuffix("...").strip()
    text = text.lstrip("*&?").strip()
    for opener, _ in (("[", "]"), ("<", ">"), ("(", ")")):
        if opener in text:
            text = text.split(opener, 1)[0].strip()
            break
    text = text.strip().strip("\"'")
    if not text or " " in text:
        return ""
    return text


def _is_stdlib_target(resolved: str, language: str | None = None) -> bool:
    return _stdlib.is_stdlib(resolved, language or "python")
