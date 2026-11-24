"""Mining job parameter defaults and validation."""

from __future__ import annotations

from typing import Any

from libref.errors import LibrefError
from libref.services.infra.jobs import INDUCE_LABELS_KIND, MINE_PATTERNS_KIND

MINE_PATTERNS_JOB_KIND = MINE_PATTERNS_KIND


INDUCE_LABELS_JOB_KIND = INDUCE_LABELS_KIND


DEFAULT_MINSUP = 2


DEFAULT_MAXLEN = 10


DEFAULT_MINLEN = 2


MIN_MINLEN = 2


def validate_mining_params(
    minsup: int, maxlen: int, minlen: int, *, topn: int | None = None
) -> None:
    """Keep sequence mining inside a bounded capacity envelope."""
    values = {"minsup": minsup, "maxlen": maxlen, "minlen": minlen}
    for name, value in values.items():
        if not isinstance(value, int) or isinstance(value, bool):
            raise LibrefError(
                "invalid_argument", f"{name} must be an integer", field=name
            )
    if minsup < DEFAULT_MINSUP:
        raise LibrefError(
            "invalid_argument",
            f"minsup must be at least {DEFAULT_MINSUP}",
            field="minsup",
        )
    if not MIN_MINLEN <= minlen <= maxlen <= DEFAULT_MAXLEN:
        raise LibrefError(
            "invalid_argument",
            f"minlen and maxlen must satisfy {MIN_MINLEN} <= minlen <= maxlen"
            f" <= {DEFAULT_MAXLEN}",
            field="maxlen",
        )
    if topn is not None and (not isinstance(topn, int) or topn < 1):
        raise LibrefError(
            "invalid_argument", "topn must be a positive integer", field="topn"
        )


def mine_job_params(
    library: str,
    version: str,
    *,
    ecosystem: str | None = None,
    minsup: int = DEFAULT_MINSUP,
    topn: int | None = None,
    maxlen: int = DEFAULT_MAXLEN,
    minlen: int = DEFAULT_MINLEN,
    include_ambiguous: bool = False,
    include_unconfirmed: bool = False,
    cluster_threshold: float | None = None,
    cluster_min_size: int | None = None,
    resolve_kinds: list[str] | tuple[str, ...] | None = None,
    min_args: int | None = None,
    max_args: int | None = None,
    profile: str | None = None,
    source_roles: list[str] | tuple[str, ...] | None = None,
    relation_kinds: list[str] | tuple[str, ...] | None = None,
    macro_policy: str | None = None,
) -> dict[str, Any]:
    """Build mining job params, symmetric with scrape job params, ensuring replayability."""
    validate_mining_params(minsup, maxlen, minlen, topn=topn)
    return {
        "library": library,
        "version": version,
        "ecosystem": ecosystem,
        "minsup": minsup,
        "topn": topn,
        "maxlen": maxlen,
        "minlen": minlen,
        "include_ambiguous": include_ambiguous,
        "include_unconfirmed": include_unconfirmed,
        "cluster_threshold": cluster_threshold,
        "cluster_min_size": cluster_min_size,
        "resolve_kinds": list(resolve_kinds) if resolve_kinds else None,
        "min_args": min_args,
        "max_args": max_args,
        "profile": profile,
        "source_roles": list(source_roles) if source_roles else None,
        "relation_kinds": list(relation_kinds) if relation_kinds else None,
        "macro_policy": macro_policy,
    }


def induce_job_params(
    library: str,
    version: str,
    *,
    ecosystem: str | None = None,
    topn: int | None = None,
    include_ambiguous: bool = False,
    include_unconfirmed: bool = False,
    profile: str | None = None,
    macro_policy: str | None = None,
) -> dict[str, Any]:
    """Build induction job params with replayable scope."""
    return {
        "library": library,
        "version": version,
        "ecosystem": ecosystem,
        "topn": topn,
        "include_ambiguous": include_ambiguous,
        "include_unconfirmed": include_unconfirmed,
        "profile": profile,
        "macro_policy": macro_policy,
    }
