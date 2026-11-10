"""PyPI registry provider: name normalization, doc candidate ranking, and payload parsing.

The PyPI-specific part of the registry subpackage; shared URL sanitization and
version logic live in the subpackage facade.
To add an ecosystem, add registry/<eco>.py and append its name to the facade's SUPPORTED_ECOSYSTEMS.
"""

from __future__ import annotations

from typing import Any

from libref.crawler.registry import (
    is_docs_like_url,
    is_repo_host_url,
    pick_repo_url,
    sanitize_http_url,
)

PYPI_DOCS_KEYS = ("documentation", "docs", "doc", "manual", "guide")


def normalize_pypi_name(name: str) -> str:
    import re

    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def rank_pypi_candidates(
    project_urls: dict[str, str] | None,
    home_page: str | None,
    *,
    allow_private: bool = False,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    urls = dict(project_urls or {})

    def push(url: str | None) -> None:
        clean = sanitize_http_url(url, allow_private=allow_private)
        if clean and clean not in seen and not is_repo_host_url(clean):
            seen.add(clean)
            out.append(clean)

    lowered = {k.lower(): v for k, v in urls.items()}
    for key in PYPI_DOCS_KEYS:
        for k, v in lowered.items():
            if key in k:
                push(v)
    for k, v in lowered.items():
        flat = k.replace(" ", "").replace("-", "").replace("_", "")
        if "changelog" in flat or "changes" in flat or "release" in flat:
            clean = sanitize_http_url(v, allow_private=allow_private)
            if clean and clean not in seen and is_docs_like_url(clean):
                seen.add(clean)
                out.append(clean)
    for k, v in lowered.items():
        if "home" in k or "site" in k or "web" in k:
            push(v)
    home = sanitize_http_url(home_page, allow_private=allow_private)
    if home and home not in seen and is_docs_like_url(home):
        seen.add(home)
        out.append(home)
    return out


def parse_pypi_payload(
    payload: dict[str, Any], *, allow_private: bool = False
) -> tuple[str | None, list[str], str | None, list[str], str | None]:
    info = payload.get("info") or {}
    stable = info.get("version")
    releases = payload.get("releases") or {}
    versions = [str(v) for v in releases]
    homepage = sanitize_http_url(info.get("home_page"), allow_private=allow_private)
    candidates = rank_pypi_candidates(
        info.get("project_urls"), info.get("home_page"), allow_private=allow_private
    )
    repo_url = pick_repo_url(info.get("project_urls"), allow_private=allow_private)
    return stable, versions, homepage, candidates, repo_url


def pypi_snapshot_url(base: str, name: str) -> str:
    return f"{base.rstrip('/')}/{name}/json"


PYPI_SEARCH_URL = "https://pypi.org/search"


def pypi_search_url(query: str) -> str:
    from urllib.parse import urlencode

    return f"{PYPI_SEARCH_URL}?{urlencode({'q': query.strip()})}"


def parse_pypi_search_html(html: str, *, limit: int = 20) -> list[dict[str, str]]:
    """Extract project names from the search result page; tolerant to snippet markup changes."""
    from selectolax.lexbor import LexborHTMLParser as HTMLParser

    if not html or not isinstance(html, str):
        return []
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001 — malformed markup yields no candidates
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        anchors = list(tree.css('a[href^="/project/"]'))
    except Exception:  # noqa: BLE001 — selector failure yields no candidates
        return []
    for node in anchors:
        try:
            href = str((node.attributes or {}).get("href", "") or "")
        except Exception:  # noqa: BLE001, S112 — unreadable node is skipped
            continue
        parts = href.strip().split("/")
        name = parts[2].strip() if len(parts) >= 3 else ""
        if not name or name in seen:
            continue
        seen.add(name)
        version = ""
        description = ""
        try:
            version_node = node.css_first(".package-snippet__version")
            if version_node is not None:
                version = (version_node.text(strip=True) or "").strip()
            desc_node = node.css_first(".package-snippet__description")
            if desc_node is not None:
                description = (desc_node.text(strip=True) or "").strip()[:300]
        except Exception:  # noqa: BLE001, S110 — snippet details are best-effort
            pass
        out.append({"name": name, "version": version, "description": description})
        if len(out) >= max(1, limit):
            break
    return out
