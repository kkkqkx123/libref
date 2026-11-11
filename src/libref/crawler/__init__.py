"""Branch A: documentation crawling (borrowing crawl4ai's algorithms, self-implemented).

Follows the blueprint crawl4ai's ideas for chained URL discovery / robots caching /
content pruning (see docs/ref/crawl4ai.md). Customization points: no crawl4ai
framework dependency, robots via stdlib robotparser, and all networking goes through
an injected httpx.AsyncClient (easy to test with MockTransport).
"""

from libref.crawler.browser import (
    close_browser,
    is_playwright_available,
    render_html,
)
from libref.crawler.extract import (
    ScrapeResult,
    convert_links_to_citations,
    extract_to_markdown,
    is_data_table,
    looks_like_js_rendered,
    scrape_html,
)
from libref.crawler.fetcher import FetchOutcome, HttpFetcher
from libref.crawler.registry import (
    ECOSYSTEM_ALIASES,
    SUPPORTED_ECOSYSTEMS,
    RegistryError,
    RegistrySnapshot,
    Resolution,
    canonical_ecosystem,
    fetch_snapshot,
    normalize_name,
    sanitize_http_url,
)
from libref.crawler.robots import RobotsChecker, ensure_robots_cache
from libref.crawler.scope import (
    FilterStats,
    ScopePolicy,
    build_scope,
    content_type_allowed,
    score_url,
    sort_by_relevance,
)
from libref.crawler.url_seeder import SeedResult, UrlSeeder

__all__ = [
    "ECOSYSTEM_ALIASES",
    "SUPPORTED_ECOSYSTEMS",
    "FetchOutcome",
    "FilterStats",
    "HttpFetcher",
    "RegistryError",
    "RegistrySnapshot",
    "Resolution",
    "RobotsChecker",
    "ScopePolicy",
    "ScrapeResult",
    "SeedResult",
    "UrlSeeder",
    "build_scope",
    "canonical_ecosystem",
    "close_browser",
    "content_type_allowed",
    "convert_links_to_citations",
    "ensure_robots_cache",
    "extract_to_markdown",
    "fetch_snapshot",
    "is_data_table",
    "is_playwright_available",
    "looks_like_js_rendered",
    "normalize_name",
    "render_html",
    "sanitize_http_url",
    "score_url",
    "scrape_html",
    "sort_by_relevance",
]
