"""crates.io provider: name normalization plus crate snapshot and search parsing.

The Rust-specific part of the registry subpackage; shared URL sanitization and
version logic live in the subpackage facade. Documentation falls back to docs.rs
version pages when the registry metadata carries no documentation address.
To add an ecosystem, add registry/<eco>.py and append its name to the facade's SUPPORTED_ECOSYSTEMS.
"""

from __future__ import annotations

import re
from typing import Any

from libref.crawler.registry import (
    is_repo_host_url,
    sanitize_http_url,
)

_CRATES_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]*$")


def normalize_crates_name(name: str) -> str:
    """Crate name normalization: trim and lowercase; registry names are ASCII with dashes and underscores."""
    text = name.strip().lower()
    if not text:
        raise ValueError("empty crate name")
    if not _CRATES_NAME_RE.match(text):
        raise ValueError(f"invalid crate name: {name!r}")
    return text


def crates_snapshot_url(base: str, name: str) -> str:
    return f"{base.rstrip('/')}/api/v1/crates/{name.strip().lower()}"


def crates_search_url(base: str, query: str, per_page: int = 20) -> str:
    from urllib.parse import urlencode

    return f"{base.rstrip('/')}/api/v1/crates?{urlencode({'q': query.strip(), 'per_page': max(1, min(per_page, 100))})}"


def docs_rs_url(name: str, version: str | None = None) -> str:
    base = f"https://docs.rs/{name.strip().lower()}"
    if version and version.strip():
        return f"{base}/{version.strip()}"
    return base


def rank_crates_candidates(
    documentation: str | None,
    name: str,
    version: str | None,
    *,
    allow_private: bool = False,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def push(url: str | None) -> None:
        clean = sanitize_http_url(url, allow_private=allow_private)
        if clean and clean not in seen and not is_repo_host_url(clean):
            seen.add(clean)
            out.append(clean)

    push(documentation)
    push(docs_rs_url(name, version))
    return out


def parse_crates_snapshot(
    payload: dict[str, Any],
    *,
    allow_private: bool = False,
    include_prerelease: bool = False,
) -> tuple[str | None, list[str], str | None, list[str], str | None]:
    """Crate metadata plus version list; yanked versions are excluded from the timeline."""
    crate = payload.get("crate") or {}
    name = str(crate.get("name") or "").strip()
    versions: list[str] = []
    for entry in payload.get("versions") or []:
        if not isinstance(entry, dict) or entry.get("yanked"):
            continue
        num = str(entry.get("num") or "").strip()
        if num and num not in versions:
            versions.append(num)
    stable = str(crate.get("max_stable_version") or "").strip() or None
    if stable is not None and stable not in versions:
        stable = None
    if stable is None:
        from libref.crawler.registry import best_effort_stable

        stable = best_effort_stable(versions)
    if include_prerelease:
        from libref.crawler.registry import best_effort_stable as _best

        stable = _best(versions, include_prerelease=True) or stable
    homepage = sanitize_http_url(crate.get("homepage"), allow_private=allow_private)
    candidates = rank_crates_candidates(
        crate.get("documentation"), name, stable, allow_private=allow_private
    )
    repo_url = sanitize_http_url(crate.get("repository"), allow_private=allow_private)
    return stable, versions, homepage, candidates, repo_url


def parse_crates_search(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten crate hits into ranking inputs; native order carries relevance."""
    out: list[dict[str, Any]] = []
    for position, item in enumerate(payload.get("crates") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        homepage = sanitize_http_url(item.get("homepage"))
        repo_url = sanitize_http_url(item.get("repository"))
        docs = sanitize_http_url(item.get("documentation"))
        docs_home = docs if docs and not is_repo_host_url(docs) else None
        out.append(
            {
                "name": name,
                "version": str(
                    item.get("max_stable_version") or item.get("max_version") or ""
                ),
                "description": str(item.get("description") or ""),
                "homepage": homepage,
                "docs_url": docs_home,
                "repo_url": repo_url,
                "popularity": item.get("downloads", 0),
                "updated": item.get("updated_at"),
                "position": position,
            }
        )
    return out
