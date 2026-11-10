"""GitHub repository search provider: stars and freshness signals for discovery ranking.

Read-only search API usage only; never clones or writes. The token travels in the
request header and comes from settings, never from storage.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from libref.crawler.registry import is_repo_host_url, sanitize_http_url

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"


def github_search_url(query: str, per_page: int) -> str:
    return f"{GITHUB_SEARCH_URL}?{urlencode({'q': query.strip(), 'per_page': max(1, min(per_page, 100))})}"


def github_search_headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    text = (token or "").strip()
    if text:
        headers["Authorization"] = f"Bearer {text}"
    return headers


def parse_github_search(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten repo items into ranking inputs; native order carries relevance."""
    out: list[dict[str, Any]] = []
    for position, item in enumerate(payload.get("items") or []):
        full_name = str(item.get("full_name") or "").strip()
        if not full_name:
            continue
        homepage = sanitize_http_url(item.get("homepage"))
        docs_home = homepage if homepage and not is_repo_host_url(homepage) else None
        out.append(
            {
                "name": full_name,
                "version": str(item.get("default_branch") or ""),
                "description": str(item.get("description") or ""),
                "homepage": docs_home,
                "repo_url": sanitize_http_url(item.get("html_url")),
                "popularity": item.get("stargazers_count", 0),
                "updated": item.get("updated_at"),
                "position": position,
            }
        )
    return out
