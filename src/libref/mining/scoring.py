"""Mining-aware retrieval scoring: pure in-memory support and cluster bonuses.

Takes already-fetched patterns and clusters, never touches the database.
"""

from __future__ import annotations

import re

_IDENT_RE = re.compile(r"[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+")
_TOKEN_RE = re.compile(r"[A-Za-z_][\w]*")


def content_idents(content: str) -> tuple[set[str], set[str]]:
    """Extract dotted identifier full names and token sets from a block."""
    full = set(_IDENT_RE.findall(content or ""))
    toks = set(_TOKEN_RE.findall(content or ""))
    return full, toks


def name_hit(name: str, full: set[str], toks: set[str]) -> bool:
    """Match a qualified name against full names or token boundaries."""
    if name in full or name in toks:
        return True
    short = name.rsplit(".", 1)[-1]
    return short in toks


def support_for_entities(
    entities: list[str], plist: list[tuple[list[str], int]]
) -> float:
    if not entities or not plist:
        return 0.0
    ceiling = max(s for _, s in plist)
    if ceiling <= 0:
        return 0.0
    have = set(entities)
    hit = max((s for names, s in plist if have.intersection(names)), default=0)
    return hit / ceiling


def cluster_hit_exact(entities: list[str], members: set[str]) -> bool:
    if not entities:
        return False
    have = set(entities)
    return len(have.intersection(members)) >= 2


def support_for_content(content: str, plist: list[tuple[list[str], int]]) -> float:
    """Max matched pattern support normalized by the version ceiling."""
    if not plist:
        return 0.0
    ceiling = max(s for _, s in plist)
    if ceiling <= 0:
        return 0.0
    full, toks = content_idents(content)
    hit = max(
        (s for names, s in plist if any(name_hit(n, full, toks) for n in names)),
        default=0,
    )
    return hit / ceiling


def cluster_hit(content: str, members: set[str]) -> bool:
    """Check whether a block covers at least two members of one cluster."""
    full, toks = content_idents(content)
    return sum(1 for n in members if name_hit(n, full, toks)) >= 2
