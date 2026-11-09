"""Branch A: documentation crawling (borrowing crawl4ai's algorithms, self-implemented).

Follows the blueprint crawl4ai's ideas for chained URL discovery / robots caching /
content pruning (see docs/ref/crawl4ai.md). Customization points: no crawl4ai
framework dependency, robots via stdlib robotparser, and all networking goes through
an injected httpx.AsyncClient (easy to test with MockTransport).
"""

from libref.crawler.browser import is_playwright_available, render_html
from libref.crawler.extract import extract_to_markdown, looks_like_js_rendered
from libref.crawler.fetcher import FetchOutcome, HttpFetcher
from libref.crawler.registry import (
    RegistryError,
    RegistrySnapshot,
    Resolution,
    fetch_snapshot,
    normalize_name,
    sanitize_http_url,
)
from libref.crawler.registry.overrides import get_override
from libref.crawler.robots import RobotsChecker, ensure_robots_cache
from libref.crawler.scope import ScopePolicy, build_scope
from libref.crawler.url_seeder import SeedResult, UrlSeeder

__all__ = [
    "FetchOutcome",
    "HttpFetcher",
    "RegistryError",
    "RegistrySnapshot",
    "Resolution",
    "is_playwright_available",
    "render_html",
    "RobotsChecker",
    "ScopePolicy",
    "SeedResult",
    "UrlSeeder",
    "build_scope",
    "ensure_robots_cache",
    "extract_to_markdown",
    "fetch_snapshot",
    "get_override",
    "looks_like_js_rendered",
    "normalize_name",
    "sanitize_http_url",
]
