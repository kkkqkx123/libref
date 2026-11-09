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
