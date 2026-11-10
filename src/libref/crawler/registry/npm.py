"""npm registry provider: name normalization plus payload parsing.

The npm-specific part of the registry subpackage; shared URL sanitization and
version logic live in the subpackage facade.
To add an ecosystem, add registry/<eco>.py and append its name to the facade's SUPPORTED_ECOSYSTEMS.
"""

from __future__ import annotations

from typing import Any

from libref.crawler.registry import is_repo_host_url, sanitize_http_url


def normalize_npm_name(name: str) -> str:
    return name.strip().lower()


def parse_npm_payload(
    payload: dict[str, Any], *, allow_private: bool = False
) -> tuple[str | None, list[str], str | None, list[str], str | None]:
    tags = payload.get("dist-tags") or {}
    stable = tags.get("latest")
    versions = [str(v) for v in (payload.get("versions") or {})]
    homepage = sanitize_http_url(payload.get("homepage"), allow_private=allow_private)
    candidates = []
    if homepage and not is_repo_host_url(homepage):
        candidates.append(homepage)
    repo_url = None
    repo = payload.get("repository") or {}
    if isinstance(repo, dict):
        repo_url = sanitize_http_url(repo.get("url"), allow_private=allow_private)
    elif isinstance(repo, str):
        repo_url = sanitize_http_url(repo, allow_private=allow_private)
    return stable, versions, homepage, candidates, repo_url


def npm_snapshot_url(base: str, name: str) -> str:
    return f"{base.rstrip('/')}/{name}"


NPM_SEARCH_URL = "https://registry.npmjs.org/-/v1/search"


def npm_search_url(query: str, size: int) -> str:
    from urllib.parse import urlencode

    return (
        f"{NPM_SEARCH_URL}?{urlencode({'text': query.strip(), 'size': max(1, size)})}"
    )


def npm_search_url_for(settings, query: str, size: int) -> str:
    """Search address derived from the artifact base so explicit mirrors apply; falls back to central."""
    from libref.crawler.registry import registry_base_for

    try:
        base = registry_base_for("npm", settings)
    except Exception:  # noqa: BLE001 — misconfigured mirror falls back to central search
        return npm_search_url(query, size)
    if not base or not base.startswith(("http://", "https://")):
        return npm_search_url(query, size)
    from urllib.parse import urlencode

    return f"{base.rstrip('/')}/-/v1/search?{urlencode({'text': query.strip(), 'size': max(1, size)})}"


def parse_npm_search(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten search objects into ranking inputs; native order carries relevance."""
    out: list[dict[str, Any]] = []
    for position, obj in enumerate(payload.get("objects") or []):
        package = obj.get("package") or {}
        name = str(package.get("name") or "").strip()
        if not name:
            continue
        score = obj.get("score") or {}
        links = package.get("links") or {}
        downloads = obj.get("downloads") or {}
        out.append(
            {
                "name": name,
                "version": str(package.get("version") or ""),
                "description": str(package.get("description") or ""),
                "homepage": links.get("homepage"),
                "repo_url": links.get("repository"),
                "popularity": downloads.get("monthly", score.get("final", 0.0)),
                "updated": package.get("date"),
                "position": position,
            }
        )
    return out
