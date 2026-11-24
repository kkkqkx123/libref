"""Deterministic discovery ranking and query normalization."""

from __future__ import annotations

from libref.services.registry.discovery.models import DiscoveryCandidate


def _normalize_query(ecosystem: str, query: str) -> str:
    text = query.strip()
    from libref.crawler.registry import canonical_ecosystem

    eco = canonical_ecosystem(ecosystem) or ecosystem.strip().lower()
    if eco == "maven":
        from libref.crawler.registry.maven import normalize_maven_name

        try:
            return normalize_maven_name(text)
        except ValueError:
            return text
    if eco == "pypi":
        from libref.crawler.registry.pypi import normalize_pypi_name

        return normalize_pypi_name(text)
    if eco == "npm":
        from libref.crawler.registry.npm import normalize_npm_name

        return normalize_npm_name(text)
    if eco == "go":
        from libref.crawler.registry.go import normalize_go_name

        try:
            return normalize_go_name(text)
        except ValueError:
            return text
    if eco == "crates":
        from libref.crawler.registry.crates import normalize_crates_name

        try:
            return normalize_crates_name(text)
        except ValueError:
            return text.lower()
    if eco == "nuget":
        from libref.crawler.registry.nuget import normalize_nuget_name

        try:
            return normalize_nuget_name(text)
        except ValueError:
            return text.lower()
    return text.lower()


_REPOLESS_NOTE = (
    "no repository address; resolve with target=repo"
    " or pass an explicit source before code indexing"
)


def _annotate(rows: list[dict]) -> list[dict]:
    for row in rows:
        if not row.get("repo_url"):
            row["note"] = _REPOLESS_NOTE
    return rows


def _popularity(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return 0.0


def _source_tier(source: str) -> int:
    return 0 if source == "registry" else 1


def rank_candidates(
    items: list[DiscoveryCandidate], query: str
) -> list[DiscoveryCandidate]:
    """Deterministic order: registry source first, then docs, exact match, native order, popularity, freshness (newest wins ties)."""
    norm = query.strip().lower()
    by_fresh = sorted(items, key=lambda c: c.updated or "", reverse=True)
    return sorted(
        by_fresh,
        key=lambda c: (
            _source_tier(c.source),
            0 if (c.docs_url or c.candidates) else 1,
            0 if c.name.lower() == norm else 1,
            c.rank,
            -_popularity(c.popularity),
        ),
    )
