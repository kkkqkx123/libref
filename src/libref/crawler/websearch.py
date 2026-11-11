"""DuckDuckGo web search provider: keyless HTML results with repo/docs classification.

Read-only HTML scraping only; never writes. Results are unverified hints:
callers confirm them before use with an explicit address. All requests go
through an injected httpx.AsyncClient.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from libref.crawler.registry import is_docs_like_url, is_repo_host_url

WEB_SEARCH_URL = "https://html.duckduckgo.com/html/"

# The results endpoint serves a bot check to non-browser agents;
# a browser user agent is required, scoped to this endpoint only.
WEB_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def web_search_url(query: str, per_page: int = 20) -> str:
    _ = max(1, min(int(per_page), 100))
    return f"{WEB_SEARCH_URL}?{urlencode({'q': query.strip()})}"


def extract_target(href: str | None) -> str | None:
    """Unwrap redirect links carrying the real target; direct links pass through."""
    if not href:
        return None
    text = href.strip()
    if text.startswith(("http://", "https://")):
        return text
    if text.startswith("//"):
        text = "https:" + text
    try:
        query = parse_qs(urlsplit(text).query)
    except ValueError:
        return None
    for value in query.get("uddg", []):
        target = unquote(value).strip()
        if target.startswith(("http://", "https://")):
            return target
    return None


def candidate_name(url: str, title: str) -> str:
    """Short display name: owner/repo for repository pages, host otherwise."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return title.strip()[:80] or url
    host = (parts.netloc or "").lower()
    segments = [seg for seg in parts.path.split("/") if seg]
    if host and segments[:1] and len(segments) >= 2 and is_repo_host_url(url):
        return f"{segments[0]}/{segments[1]}"
    if host:
        return host.removeprefix("www.")
    return title.strip()[:80] or url


def classify_target(url: str) -> str:
    if is_repo_host_url(url):
        return "repo"
    if is_docs_like_url(url):
        return "docs"
    return "other"


def parse_ddg_html(html: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Extract title, target and snippet per result block; tolerant to markup drift."""
    from selectolax.lexbor import LexborHTMLParser as HTMLParser

    if not html or not isinstance(html, str):
        return []
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001 — malformed markup yields no candidates
        return []
    out: list[dict[str, Any]] = []
    try:
        blocks = list(tree.css("div.result"))
    except Exception:  # noqa: BLE001 — selector failure yields no candidates
        return []
    for block in blocks:
        try:
            anchor = block.css_first("a.result__a")
        except Exception:  # noqa: BLE001, S112 — unreadable block is skipped
            continue
        if anchor is None:
            continue
        try:
            href = str((anchor.attributes or {}).get("href", "") or "")
            title = (anchor.text(strip=True) or "").strip()
        except Exception:  # noqa: BLE001, S112 — unreadable block is skipped
            continue
        target = extract_target(href)
        if not target:
            continue
        snippet = ""
        try:
            snippet_node = block.css_first("a.result__snippet")
            if snippet_node is not None:
                snippet = (snippet_node.text(strip=True) or "").strip()
        except Exception:  # noqa: BLE001 — snippet is best-effort
            snippet = ""
        out.append(
            {
                "title": title,
                "url": target,
                "snippet": snippet,
                "position": len(out),
            }
        )
        if len(out) >= max(1, limit):
            break
    return out
