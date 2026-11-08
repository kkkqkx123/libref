"""分支 A：文档抓取（借鉴 crawl4ai 算法，自研实现）。

蓝本 crawl4ai 的 URL 链式发现 / robots 缓存 / 正文净化思路（见 docs/ref/crawl4ai.md），
定制点：不引入 crawl4ai 框架依赖，robots 用 stdlib robotparser，网络统一走注入的
httpx.AsyncClient（便于测试 MockTransport）。
"""

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
from libref.crawler.registry_overrides import get_override
from libref.crawler.robots import RobotsChecker, ensure_robots_cache
from libref.crawler.scope import ScopePolicy, build_scope
from libref.crawler.url_seeder import SeedResult, UrlSeeder

__all__ = [
    "FetchOutcome",
    "HttpFetcher",
    "RegistryError",
    "RegistrySnapshot",
    "Resolution",
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
