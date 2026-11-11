"""NuGet provider: case-insensitive names plus registration snapshot and query parsing.

The .NET-specific part of the registry subpackage; shared URL sanitization and
version logic live in the subpackage facade. Versions come from the registration
index timeline; documentation prefers the package project URL and falls back to
the nuget.org package page.
To add an ecosystem, add registry/<eco>.py and append its name to the facade's SUPPORTED_ECOSYSTEMS.
"""

from __future__ import annotations

from typing import Any

from libref.crawler.registry import (
    is_repo_host_url,
    sanitize_http_url,
)

NUGET_SEARCH_URL = "https://azuresearch-usnc.nuget.org/query"


def normalize_nuget_name(name: str) -> str:
    """Package id normalization: trim; ids are case-insensitive so lookups use lowercase."""
    text = name.strip()
    if not text:
        raise ValueError("empty nuget package id")
    if any(ch.isspace() for ch in text):
        raise ValueError(f"invalid nuget package id: {name!r}")
    return text.lower()


def nuget_snapshot_url(base: str, name: str) -> str:
    return f"{base.rstrip('/')}/{name.strip().lower()}/index.json"


def nuget_package_url(name: str, version: str | None = None) -> str:
    base = f"https://www.nuget.org/packages/{name.strip()}"
    if version and version.strip():
        return f"{base}/{version.strip()}"
    return base


def nuget_search_url(query: str, take: int = 20) -> str:
    from urllib.parse import urlencode

    return f"{NUGET_SEARCH_URL}?{urlencode({'q': query.strip(), 'take': max(1, min(take, 100))})}"


def _is_prerelease(version: str) -> bool:
    lowered = version.lower()
    return "-" in lowered


def _timeline_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for page in payload.get("items") or []:
        if not isinstance(page, dict):
            continue
        inline = page.get("items")
        if isinstance(inline, list):
            for item in inline:
                if isinstance(item, dict) and isinstance(
                    item.get("catalogEntry"), dict
                ):
                    entries.append(item["catalogEntry"])
    return entries


def nuget_page_urls(payload: dict[str, Any], *, limit: int = 2) -> list[str]:
    """Registration page addresses when entries are not inlined in the index."""
    urls: list[str] = []
    for page in payload.get("items") or []:
        if not isinstance(page, dict):
            continue
        if isinstance(page.get("items"), list):
            continue
        address = page.get("@id")
        if isinstance(address, str) and address.strip() and address not in urls:
            urls.append(address.strip())
        if len(urls) >= max(1, limit):
            break
    return urls


def pick_nuget_stable(
    versions: list[str], *, include_prerelease: bool = False
) -> str | None:
    """Latest non-prerelease in timeline order; prerelease only when nothing else exists.

    With inclusion enabled the timeline maximum wins directly, prereleases included."""
    if include_prerelease:
        return versions[-1] if versions else None
    for version in reversed(versions):
        if not _is_prerelease(version):
            return version
    return versions[-1] if versions else None


def parse_nuget_registration(
    payload: dict[str, Any], *, allow_private: bool = False
) -> tuple[list[str], str | None]:
    """Registration timeline fragment: versions in order plus the first project URL seen."""
    versions: list[str] = []
    project_url: str | None = None
    for entry in _timeline_entries(payload):
        version = str(entry.get("version") or "").strip()
        if version and version not in versions:
            versions.append(version)
        if project_url is None:
            candidate = str(entry.get("projectUrl") or "").strip()
            if candidate:
                project_url = candidate
    return versions, sanitize_http_url(project_url, allow_private=allow_private)


def rank_nuget_candidates(
    project_url: str | None,
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

    push(project_url)
    push(nuget_package_url(name, version))
    return out


def parse_nuget_search(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten query hits into ranking inputs; native order carries relevance."""
    out: list[dict[str, Any]] = []
    for position, item in enumerate(payload.get("data") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("id") or "").strip()
        if not name:
            continue
        project_url = sanitize_http_url(item.get("projectUrl"))
        out.append(
            {
                "name": name,
                "version": str(item.get("version") or ""),
                "description": str(item.get("description") or ""),
                "homepage": project_url,
                "repo_url": None,
                "popularity": item.get("totalDownloads", 0),
                "updated": None,
                "position": position,
            }
        )
    return out
