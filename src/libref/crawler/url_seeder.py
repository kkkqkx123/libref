"""Chained URL discovery: robots → llms.txt → sitemap (.gz/index) (blueprint: crawl4ai AsyncUrlSeeder pattern).

Customization points: all requests go through an injected httpx.AsyncClient
(MockTransport for tests, no network); llms.txt takes precedence over sitemap;
same-site restriction + robots Disallow filtering + deduplication; a HEAD
liveness pre-check is an optional step.
"""

from __future__ import annotations

import gzip
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from libref.crawler.robots import RobotsChecker
from libref.crawler.scope import (
    content_type_allowed,
    crawl_key,
    score_url,
    sort_by_relevance,
)

_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")

_SITEMAP_NAMES = ("sitemap.xml", "sitemap_index.xml", "sitemap-index.xml")
_SITEMAP_GZ_NAMES = ("sitemap.xml.gz", "sitemap_index.xml.gz", "sitemap-index.xml.gz")
_FEED_NAMES = ("feed.xml", "atom.xml", "rss.xml", "feed")
_LLMS_VARIANTS = ("llms.txt", "llms-full.txt")
_MAX_SITEMAP_DEPTH = 3
_MAX_SITEMAP_URLS = 20000

# Utility addresses that are never documentation: seeder self-descriptions and
# common static resources. Mirrors the blueprint nonsense-URL filter idea.
_NONSENSE_PATH_SUFFIXES = (
    "/robots.txt",
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/ads.txt",
    "/humans.txt",
    "/security.txt",
    "/crossdomain.xml",
    "/browserconfig.xml",
    "/manifest.json",
    "/favicon.ico",
    "/apple-touch-icon.png",
)
_NONSENSE_SEGMENTS = (
    "/sitemap",
    "/.well-known/",
)

# Process-local seed cache: start URL → (stored_at, result). Bounded and TTL-guarded.
_SEED_CACHE: dict[str, tuple[float, "SeedResult"]] = {}  # noqa: UP037 — SeedResult is defined below
_SEED_CACHE_CAP = 256


def _is_nonsense_url(url: str) -> bool:
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return True
    if path.endswith(_NONSENSE_PATH_SUFFIXES):
        return True
    return any(seg in path for seg in _NONSENSE_SEGMENTS)


def _clean_link_token(token: str) -> str:
    cleaned = token.strip().strip("<\"'([")
    cleaned = cleaned.rstrip(".,;:!?\"'`)]}>")
    if cleaned.endswith(")") and "(" not in cleaned:
        cleaned = cleaned[:-1]
    return cleaned.strip()


def _looks_like_doc_path(token: str) -> bool:
    if not token or " " in token or ":" in token:
        return False
    if token.startswith((".", "/")):
        return True
    lowered = token.lower()
    if lowered.endswith((".md", ".html", ".htm", ".txt", ".mdx")):
        return True
    return "/" in token and len(token) < 256


@dataclass
class SeedResult:
    urls: list[str] = field(default_factory=list)
    source: str = "none"  # llms.txt / sitemap / none
    attempted: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    truncated: bool = False
    failed: bool = False
    cross_site: list[str] = field(default_factory=list)


class UrlSeeder:
    def __init__(
        self,
        client: httpx.AsyncClient,
        robots: RobotsChecker | None,
        cache_ttl_hours: float = 0.0,
    ) -> None:
        self._client = client
        self._robots = robots
        self._cache_ttl = max(0.0, cache_ttl_hours) * 3600

    async def discover(
        self,
        start_url: str,
        *,
        prefilter: bool = False,
        max_pages: int | None = None,
        query: str | None = None,
        score_threshold: float | None = None,
        force: bool = False,
    ) -> SeedResult:
        """Discover doc URLs from the start URL; llms.txt first, then the sitemap queue, then feed fallback."""
        if not force and self._cache_ttl > 0:
            hit = _SEED_CACHE.get(start_url)
            if hit is not None and time.time() - hit[0] < self._cache_ttl:
                return hit[1]
        parts = urlsplit(start_url)
        host = parts.netloc

        llms = await self._collect_llms(start_url)
        if llms.urls:
            result = llms
            attempted = list(llms.attempted)
            errors = dict(llms.errors)
            truncated = llms.truncated
            transient = False
        else:
            sitemap = await self._collect_sitemap(start_url)
            if sitemap.urls:
                result = sitemap
                attempted = list(dict.fromkeys(llms.attempted + sitemap.attempted))
                errors = {**llms.errors, **sitemap.errors}
                truncated = llms.truncated or sitemap.truncated
                transient = llms.failed or sitemap.failed
            else:
                feed = await self._collect_feed(start_url)
                result = feed if feed.urls else sitemap
                attempted = list(
                    dict.fromkeys(llms.attempted + sitemap.attempted + feed.attempted)
                )
                errors = {**llms.errors, **sitemap.errors, **feed.errors}
                truncated = llms.truncated or sitemap.truncated or feed.truncated
                transient = llms.failed or sitemap.failed or feed.failed

        seen: set[str] = set()
        urls: list[str] = []
        cross_site: list[str] = []
        for url in result.urls:
            key = crawl_key(url)
            if key in seen:
                continue
            if _is_nonsense_url(url) or not content_type_allowed(url, None):
                continue
            if urlsplit(url).netloc != host:
                if url not in cross_site:
                    cross_site.append(url)
                continue
            if self._robots is not None and not await self._robots.can_fetch(url):
                continue
            if prefilter and not await self._head_ok(url):
                continue
            seen.add(key)
            urls.append(url)
        if query and query.strip():
            if score_threshold is not None:
                urls = [u for u in urls if score_url(u, query) >= score_threshold]
            urls = sort_by_relevance(urls, query)
        if max_pages is not None and len(urls) > max_pages:
            # The count cap applies only to the filtered set; whether more candidates remain decides the truncated flag
            truncated = True
            urls = urls[:max_pages]

        if not urls:
            outcome = SeedResult(
                urls=[],
                source="none",
                attempted=attempted,
                errors=errors,
                truncated=truncated,
                failed=transient,
                cross_site=cross_site,
            )
        else:
            outcome = SeedResult(
                urls=urls,
                source=result.source,
                attempted=attempted,
                errors=errors,
                truncated=truncated,
                failed=False,
                cross_site=cross_site,
            )
        if self._cache_ttl > 0:
            while len(_SEED_CACHE) >= _SEED_CACHE_CAP:
                oldest = next(iter(_SEED_CACHE))
                del _SEED_CACHE[oldest]
            _SEED_CACHE[start_url] = (time.time(), outcome)
        return outcome

    # ── llms.txt (with directory-level fallback and doc variants) ──

    async def _from_llms_txt(self, base: str) -> SeedResult:
        return await self._collect_llms(base)

    async def _collect_llms(self, start_url: str) -> SeedResult:
        # Every level from the start directory up to the root is read: on
        # multi-product sites a section file shadows the product file, and the
        # fuller listing is the corpus, not the first hit. Single-level sites
        # behave exactly as before.
        attempted: list[str] = []
        errors: dict[str, str] = {}
        transient_seen = False
        best: dict[str, tuple[list[str], list[str]]] = {}
        for directory in _llms_directories(start_url):
            for variant in _LLMS_VARIANTS:
                candidate = f"{directory}/{variant}"
                attempted.append(candidate)
                body, note, transient = await self._get_text(candidate)
                if body is None:
                    errors[candidate] = note or "fetch failed"
                    transient_seen = transient_seen or transient
                    continue
                urls, cross = self._parse_llms_body(body, candidate, start_url)
                if len(urls) > len(best.get(variant, ([], []))[0]):
                    best[variant] = (urls, cross)
                errors[candidate] = f"{len(urls)} urls" if urls else "no urls"
        best_urls: list[str] = []
        best_cross: list[str] = []
        for variant in _LLMS_VARIANTS:
            if variant in best:
                best_urls, best_cross = best[variant]
                break
        if best_urls:
            return SeedResult(
                urls=best_urls + best_cross,
                source="llms.txt",
                attempted=attempted,
                errors=errors,
                cross_site=[],
            )
        return SeedResult(
            attempted=attempted,
            errors=errors,
            failed=transient_seen,
        )

    def _parse_llms_body(
        self, body: str, llms_url: str, start_url: str
    ) -> tuple[list[str], list[str]]:
        """Parse an llms list; explicit link targets are all kept (relative ones normalized against the llms URL), bare words only if doc-shaped."""
        host = urlsplit(start_url).netloc
        urls: list[str] = []
        cross: list[str] = []
        seen: set[str] = set()

        def _emit(raw: str) -> None:
            cleaned = _clean_link_token(raw)
            if not cleaned or cleaned.startswith(("#", "mailto:", "data:")):
                return
            if cleaned.startswith(("http://", "https://")):
                norm = cleaned
            elif cleaned.startswith("/"):
                norm = urljoin(llms_url, cleaned)
            else:
                # Relative paths and bare relative links are normalized against the llms URL
                if ":" in cleaned and not cleaned.startswith((".", "/")):
                    return
                norm = urljoin(llms_url, cleaned)
            if urlsplit(norm).netloc != host:
                if norm not in cross and norm not in seen:
                    cross.append(norm)
                return
            key = crawl_key(norm)
            if key not in seen:
                seen.add(key)
                urls.append(key)

        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith(("- ", "* ", "+ ")):
                content = stripped[2:].strip()
            else:
                numbered = re.match(r"\d+\.\s+(.*)", stripped)
                if not numbered:
                    continue
                content = numbered.group(1).strip()
            link_targets = [_clean_link_token(t) for t in _LINK_RE.findall(content)]
            for target in link_targets:
                if not target or target.startswith(("#", "mailto:", "data:")):
                    continue
                _emit(target)
            remainder = _LINK_RE.sub(" ", content)
            for token in remainder.split():
                cleaned = _clean_link_token(token)
                if not cleaned or cleaned.startswith(("#", "mailto:", "data:")):
                    continue
                if cleaned.startswith(
                    ("http://", "https://", "/")
                ) or _looks_like_doc_path(cleaned):
                    _emit(cleaned)
        return urls, cross

    # ── sitemap (incl. robots declarations, multi-level indexes, magic-number decompression) ──

    async def _from_sitemap(self, base: str, depth: int = 0) -> SeedResult:
        return await self._collect_sitemap(base, depth=depth)

    async def _collect_sitemap(self, start_url: str, depth: int = 0) -> SeedResult:
        attempted: list[str] = []
        errors: dict[str, str] = {}
        transient_seen = False
        truncated = False
        for candidate in await self._sitemap_candidates(start_url):
            attempted.append(candidate)
            body, note, transient = await self._get_bytes(candidate)
            if body is None:
                errors[candidate] = note or "fetch failed"
                transient_seen = transient_seen or transient
                continue
            try:
                payload = _maybe_gunzip(body)
            except OSError as exc:
                errors[candidate] = f"gzip error: {exc}"
                continue
            text = payload.decode("utf-8", errors="replace")
            urls, child_truncated = await self._expand_sitemap_text(
                text,
                candidate,
                depth,
                attempted,
                errors,
            )
            truncated = truncated or child_truncated
            if urls:
                capped, capped_truncated = _cap_urls(urls)
                return SeedResult(
                    urls=capped,
                    source="sitemap",
                    attempted=attempted,
                    errors=errors,
                    truncated=truncated or capped_truncated,
                )
            errors[candidate] = "no urls"
        return SeedResult(
            attempted=attempted,
            errors=errors,
            truncated=truncated,
            failed=transient_seen,
        )

    async def _sitemap_candidates(self, start_url: str) -> list[str]:
        parts = urlsplit(start_url)
        root = f"{parts.scheme}://{parts.netloc}"
        queue: list[str] = []
        if self._robots is not None:
            try:
                declared = await self._robots.sitemap_urls(start_url)
            except Exception:  # noqa: BLE001 — degrade to direct URLs when robots is unreachable; never block discovery
                declared = []
            for loc in declared:
                if loc not in queue:
                    queue.append(loc)
        dirs = _sitemap_directories(start_url)
        for name in _SITEMAP_NAMES:
            for directory in dirs:
                candidate = f"{directory}/{name}"
                if candidate not in queue:
                    queue.append(candidate)
        for name in _SITEMAP_GZ_NAMES:
            for directory in dirs:
                candidate = f"{directory}/{name}"
                if candidate not in queue:
                    queue.append(candidate)
        # Root fallback: when the start URL is odd, at least try the site root
        if not queue:
            queue.append(f"{root}/sitemap.xml")
        return queue

    async def _expand_sitemap_text(
        self,
        text: str,
        base_url: str,
        depth: int,
        attempted: list[str],
        errors: dict[str, str],
    ) -> tuple[list[str], bool]:
        if "<sitemapindex" not in text:
            return _cap_urls(self._parse_loc_tags(text))
        if depth >= _MAX_SITEMAP_DEPTH:
            return [], True
        nested: list[str] = []
        for child in self._parse_sitemap_locs(text):
            child_url = child if child.startswith("http") else urljoin(base_url, child)
            attempted.append(child_url)
            body, note, _ = await self._get_bytes(child_url)
            if body is None:
                errors[child_url] = note or "fetch failed"
                continue
            try:
                payload = _maybe_gunzip(body)
            except OSError as exc:
                errors[child_url] = f"gzip error: {exc}"
                continue
            child_text = payload.decode("utf-8", errors="replace")
            if "<sitemapindex" in child_text:
                deeper, _ = await self._expand_sitemap_text(
                    child_text,
                    child_url,
                    depth + 1,
                    attempted,
                    errors,
                )
                nested.extend(deeper)
            else:
                nested.extend(self._parse_loc_tags(child_text))
            if len(nested) >= _MAX_SITEMAP_URLS:
                return _cap_urls(nested)
        return _cap_urls(nested)

    async def _get_text(self, url: str) -> tuple[str | None, str | None, bool]:
        body, note, transient = await self._get_bytes(url)
        if body is None:
            return None, note, transient
        try:
            payload = _maybe_gunzip(body)
        except OSError:
            # llms is plain text: on magic-number mismatch just decode as-is
            payload = body
        return payload.decode("utf-8", errors="replace"), None, False

    async def _get_bytes(self, url: str) -> tuple[bytes | None, str | None, bool]:
        from libref.crawler.http import resilient_get

        try:
            resp = await resilient_get(self._client, url)
        except httpx.HTTPError as exc:
            return None, f"{type(exc).__name__}: {exc}", True
        if resp.status_code == 429 or resp.status_code >= 500:
            return None, f"HTTP {resp.status_code}", True
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}", False
        return resp.content, None, False

    async def _collect_feed(self, start_url: str) -> SeedResult:
        """Feed fallback: try conventional feed paths; successful hits source as feed."""
        attempted: list[str] = []
        errors: dict[str, str] = {}
        transient_seen = False
        for candidate in _feed_candidates(start_url):
            attempted.append(candidate)
            body, note, transient = await self._get_bytes(candidate)
            if body is None:
                errors[candidate] = note or "fetch failed"
                transient_seen = transient_seen or transient
                continue
            try:
                payload = _maybe_gunzip(body)
            except OSError:
                payload = body
            text = payload.decode("utf-8", errors="replace")
            urls = _parse_feed_urls(text, candidate)
            if urls:
                capped, capped_truncated = _cap_urls(urls)
                return SeedResult(
                    urls=capped,
                    source="feed",
                    attempted=attempted,
                    errors=errors,
                    truncated=capped_truncated,
                )
            errors[candidate] = "no urls"
        return SeedResult(
            attempted=attempted,
            errors=errors,
            truncated=False,
            failed=transient_seen,
        )

    async def _head_ok(self, url: str) -> bool:
        try:
            resp = await self._client.head(url, follow_redirects=True)
        except httpx.HTTPError:
            return False
        if resp.status_code >= 400:
            return False
        ctype = resp.headers.get("content-type", "")
        if not content_type_allowed(url, ctype):
            return False
        allowed = ("text/html", "text/plain", "application/xhtml", "text/markdown")
        return any(a in ctype for a in allowed)

    @staticmethod
    def _parse_loc_tags(xml_text: str) -> list[str]:
        """Tolerant <loc> tag parsing (no XML dependency; sitemap structure is fixed)."""
        urls: list[str] = []
        for chunk in xml_text.split("<loc>")[1:]:
            end = chunk.find("</loc>")
            if end > 0:
                loc = chunk[:end].strip()
                if loc:
                    urls.append(loc)
        return urls

    @staticmethod
    def _parse_sitemap_locs(xml_text: str) -> list[str]:
        """List of <sitemap><loc> sub-sitemaps in a sitemapindex."""
        locs: list[str] = []
        for chunk in xml_text.split("<sitemap>")[1:]:
            end = chunk.find("</sitemap>")
            if end < 0:
                continue
            block = chunk[:end]
            if "<loc>" in block:
                loc = block.split("<loc>")[1].split("</loc>")[0].strip()
                if loc:
                    locs.append(loc)
        return locs


def _maybe_gunzip(body: bytes) -> bytes:
    if body[:2] == b"\x1f\x8b":
        return gzip.decompress(body)
    return body


def _cap_urls(urls: list[str]) -> tuple[list[str], bool]:
    if len(urls) > _MAX_SITEMAP_URLS:
        return urls[:_MAX_SITEMAP_URLS], True
    return urls, len(urls) >= _MAX_SITEMAP_URLS


def _llms_directories(start_url: str) -> list[str]:
    parts = urlsplit(start_url)
    root = f"{parts.scheme}://{parts.netloc}"
    segments = [s for s in parts.path.split("/") if s and s != ""]
    if parts.path.endswith("/"):
        dirs_from_start = [root + "/" + "/".join(segments)] if segments else [root]
    else:
        page_dir = segments[:-1]
        dirs_from_start = [root + "/" + "/".join(page_dir)] if page_dir else [root]
        # No guessing about suffix-less sibling directories for file starts; fall back by parent directory only
    out: list[str] = []
    current = dirs_from_start[0]
    while True:
        out.append(current.rstrip("/") if current != root else root)
        if current == root:
            break
        parent_parts = urlsplit(current)
        segs = [s for s in parent_parts.path.split("/") if s]
        if not segs:
            current = root
        else:
            current = root + "/" + "/".join(segs[:-1]) if len(segs) > 1 else root
    # Deduplicate, preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for directory in out:
        normalized = directory.rstrip("/") or root
        if normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def _sitemap_directories(start_url: str) -> list[str]:
    parts = urlsplit(start_url)
    root = f"{parts.scheme}://{parts.netloc}"
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return [root]
    if parts.path.endswith("/"):
        start_dir = root + "/" + "/".join(segments)
    else:
        start_dir = root + "/" + "/".join(segments[:-1]) if len(segments) > 1 else root
    dirs = [root]
    if start_dir != root and start_dir not in dirs:
        dirs.append(start_dir.rstrip("/"))
        # Fill in intermediate parent directories for deep starts so every level is tried
        current = start_dir.rstrip("/")
        while current != root:
            parent_parts = urlsplit(current)
            segs = [s for s in parent_parts.path.split("/") if s]
            current = root + "/" + "/".join(segs[:-1]) if len(segs) > 1 else root
            if current not in dirs and current != start_dir:
                dirs.append(current)
    return dirs


_FEED_LINK_HREF_RE = re.compile(
    r"<link[^>]+href\s*=\s*[\"']([^\"'<>\s]+)[\"']", re.IGNORECASE
)
_FEED_LINK_TEXT_RE = re.compile(r"<link>([^<>]+)</link>", re.IGNORECASE)
_FEED_GUID_RE = re.compile(r"<guid[^>]*>([^<>]+)</guid>", re.IGNORECASE)
_FEED_ENCLOSURE_RE = re.compile(
    r"<enclosure[^>]+url\s*=\s*[\"']([^\"'<>\s]+)[\"']", re.IGNORECASE
)


def _feed_candidates(start_url: str) -> list[str]:
    """Conventional feed paths under each sitemap directory; order-preserving dedup."""
    out: list[str] = []
    seen: set[str] = set()
    for directory in _sitemap_directories(start_url):
        base = directory.rstrip("/")
        for name in _FEED_NAMES:
            candidate = f"{base}/{name}"
            if candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


def _parse_feed_urls(text: str, base_url: str) -> list[str]:
    """Tolerant feed link extraction: Atom href, RSS link text, guid, enclosure."""
    found: list[str] = []
    for pattern in (
        _FEED_LINK_HREF_RE,
        _FEED_LINK_TEXT_RE,
        _FEED_GUID_RE,
        _FEED_ENCLOSURE_RE,
    ):
        for match in pattern.findall(text or ""):
            token = _clean_link_token(str(match))
            if not token or token.startswith(("#", "mailto:", "data:")):
                continue
            if token.startswith(("http://", "https://")):
                found.append(token)
            elif token.startswith("/") or _looks_like_doc_path(token):
                found.append(urljoin(base_url, token))
    seen: set[str] = set()
    ordered: list[str] = []
    for url in found:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered
