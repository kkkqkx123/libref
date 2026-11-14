"""Crawl scope policy: the start URL and user options decide which URLs are kept. Pure logic, zero networking."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


class BudgetExceeded(RuntimeError):
    """Loud budget stop: the candidate set outgrew its declared budget, so the run fails instead of silently returning a subset."""


# Default binary suffixes rejected before any request is spent; override via
# excluded_suffixes / allowed_suffixes options. Mirrors the blueprint
# ContentTypeFilter idea of refusing non-document payloads at decision time.
_DEFAULT_EXCLUDED_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".bmp",
    ".mp4",
    ".mp3",
    ".wav",
    ".ogg",
    ".webm",
    ".avi",
    ".mov",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".otf",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".rar",
    ".7z",
    ".exe",
    ".dmg",
    ".pkg",
    ".deb",
    ".rpm",
    ".pdf",
)


@dataclass
class FilterStats:
    """Per-run scope counters; observability only, never affects decisions."""

    accepted: int = 0
    rejected: int = 0
    content_type_rejected: int = 0


#
# Query parameters that only point inside the document they are attached to.
# A URL carrying nothing but these names is the same document as its bare path:
# anchor-listing sources such as `llms.txt` enumerate one page once per heading.
_ANCHOR_QUERY_KEYS = frozenset({"id", "anchor", "section", "highlight", "hl"})
_TRACKING_QUERY_PREFIXES = ("utm_",)
_TRACKING_QUERY_KEYS = frozenset({"ref", "source", "referrer"})


def crawl_key(url: str) -> str:
    """Identity of the document behind a URL, for crawl deduplication.

    The fragment never reaches the server, and a query that names only an anchor
    or a tracking parameter selects no other document than the bare path: both
    halves are dropped so the crawl spends its page budget once per page and
    stores one body per page instead of one per heading.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    keys = [
        key
        for key, _value in (
            pair.split("=", 1) if "=" in pair else (pair, "")
            for pair in parts.query.split("&")
            if pair
        )
    ]
    if keys and not all(
        key.lower() in _ANCHOR_QUERY_KEYS
        or key.lower() in _TRACKING_QUERY_KEYS
        or key.lower().startswith(_TRACKING_QUERY_PREFIXES)
        for key in keys
    ):
        return url.split("#", 1)[0]
    bare = parts._replace(query="", fragment="").geturl()
    return bare


def content_type_allowed(url: str, content_type: str | None) -> bool:
    """Suffix-first, MIME-second document check shared by scope filtering and HEAD prescreening."""
    lowered = (url or "").lower().split("?")[0]
    if lowered.endswith(_DEFAULT_EXCLUDED_SUFFIXES):
        return False
    if not content_type:
        return True
    ctype = content_type.lower().split(";")[0].strip()
    if ctype.startswith(("image/", "audio/", "video/", "font/")):
        return False
    return ctype not in (
        "application/zip",
        "application/gzip",
        "application/x-tar",
        "application/pdf",
        "application/octet-stream",
    )


def score_url(url: str, query: str) -> float:
    """URL-only relevance in 0..1: query token overlap over host and path, penalized by depth."""
    tokens = [t for t in query.lower().split() if t]
    if not tokens:
        return 0.0
    try:
        parts = urlsplit(url)
    except ValueError:
        return 0.0
    host = parts.netloc.lower().replace("www.", "")
    segments = [s.lower() for s in parts.path.split("/") if s]
    haystack = " ".join([host, *segments, parts.query.lower()])
    hits = sum(1 for t in tokens if t in haystack)
    if hits == 0:
        return 0.0
    depth_penalty = 1.0 / (1.0 + 0.1 * max(0, len(segments) - 2))
    return min(1.0, (hits / len(tokens)) * depth_penalty)


def sort_by_relevance(urls: list[str], query: str | None) -> list[str]:
    """Stable relevance ordering; empty query keeps discovery order."""
    if not query or not query.strip():
        return list(urls)
    scored = [(score_url(u, query), i, u) for i, u in enumerate(urls)]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [u for _, _, u in scored]


@dataclass
class ScopePolicy:
    """Takes a start URL and explicit options, decides whether to accept a candidate URL.

    Defaults match historical behavior: same-site only, cross-site rejected;
    explicit options apply only when given.
    """

    start_url: str
    allow_cross_site: bool = False
    allow_subdomains: bool = False
    path_prefixes: tuple[str, ...] = ()
    path_suffixes: tuple[str, ...] = ()
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    max_depth: int | None = None
    excluded_suffixes: tuple[str, ...] = ()
    allowed_suffixes: tuple[str, ...] = ()
    _include_res: tuple[re.Pattern[str], ...] = field(default=(), repr=False)
    _exclude_res: tuple[re.Pattern[str], ...] = field(default=(), repr=False)

    def __init__(
        self,
        start_url: str,
        *,
        allow_cross_site: bool = False,
        allow_subdomains: bool = False,
        path_prefixes: tuple[str, ...] | list[str] = (),
        path_suffixes: tuple[str, ...] | list[str] = (),
        include_patterns: tuple[str, ...] | list[str] = (),
        exclude_patterns: tuple[str, ...] | list[str] = (),
        max_depth: int | None = None,
        excluded_suffixes: tuple[str, ...] | list[str] | None = None,
        allowed_suffixes: tuple[str, ...] | list[str] = (),
    ) -> None:
        object.__setattr__(self, "start_url", start_url)
        object.__setattr__(self, "allow_cross_site", bool(allow_cross_site))
        object.__setattr__(self, "allow_subdomains", bool(allow_subdomains))
        object.__setattr__(self, "path_prefixes", tuple(path_prefixes or ()))
        object.__setattr__(self, "path_suffixes", tuple(path_suffixes or ()))
        object.__setattr__(self, "include_patterns", tuple(include_patterns or ()))
        object.__setattr__(self, "exclude_patterns", tuple(exclude_patterns or ()))
        object.__setattr__(self, "max_depth", max_depth)
        object.__setattr__(
            self,
            "excluded_suffixes",
            tuple(
                s.lower() if s.startswith(".") else f".{s.lower()}"
                for s in (
                    excluded_suffixes
                    if excluded_suffixes is not None
                    else _DEFAULT_EXCLUDED_SUFFIXES
                )
            ),
        )
        object.__setattr__(
            self,
            "allowed_suffixes",
            tuple(
                s.lower() if s.startswith(".") else f".{s.lower()}"
                for s in (allowed_suffixes or ())
            ),
        )
        object.__setattr__(
            self,
            "_include_res",
            tuple(re.compile(p) for p in (include_patterns or ())),
        )
        object.__setattr__(
            self,
            "_exclude_res",
            tuple(re.compile(p) for p in (exclude_patterns or ())),
        )

    def accept(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if not parts.scheme.startswith("http"):
            return False
        base = urlsplit(self.start_url)
        if not self.allow_cross_site and not _host_ok(
            parts.netloc.lower(),
            base.netloc.lower(),
            self.allow_subdomains,
        ):
            return False
        path = parts.path or "/"
        lowered_path = path.lower().split("?")[0]
        if self.allowed_suffixes:
            if not any(lowered_path.endswith(s) for s in self.allowed_suffixes):
                return False
        elif self.excluded_suffixes and any(
            lowered_path.endswith(s) for s in self.excluded_suffixes
        ):
            return False
        if self.path_prefixes and not any(
            path.startswith(p) for p in self.path_prefixes
        ):
            return False
        if self.path_suffixes and not any(path.endswith(s) for s in self.path_suffixes):
            return False
        for rx in self._exclude_res:
            if rx.search(url):
                return False
        if self._include_res and not any(rx.search(url) for rx in self._include_res):
            return False
        if self.max_depth is None:
            return True
        return _depth_ok(self.start_url, url, self.max_depth)

    def filter(self, urls: list[str]) -> tuple[list[str], list[str]]:
        accepted: list[str] = []
        rejected: list[str] = []
        for url in urls:
            (accepted if self.accept(url) else rejected).append(url)
        return accepted, rejected

    def filter_with_stats(
        self, urls: list[str]
    ) -> tuple[list[str], list[str], FilterStats]:
        stats = FilterStats()
        accepted: list[str] = []
        rejected: list[str] = []
        for url in urls:
            if self.accept(url):
                stats.accepted += 1
                accepted.append(url)
            else:
                stats.rejected += 1
                try:
                    lowered = urlsplit(url).path.lower()
                except ValueError:
                    lowered = ""
                if any(
                    lowered.split("?")[0].endswith(s) for s in self.excluded_suffixes
                ):
                    stats.content_type_rejected += 1
                rejected.append(url)
        return accepted, rejected, stats


def build_scope(start_url: str, opts: dict | None) -> ScopePolicy:
    """Build the policy from a task options dict; empty options yield the default same-site policy."""
    opts = opts or {}
    return ScopePolicy(
        start_url,
        allow_cross_site=bool(opts.get("allow_cross_site", False)),
        allow_subdomains=bool(opts.get("allow_subdomains", False)),
        path_prefixes=tuple(opts.get("path_prefixes", ()) or ()),
        path_suffixes=tuple(opts.get("path_suffixes", ()) or ()),
        include_patterns=tuple(opts.get("include_patterns", ()) or ()),
        exclude_patterns=tuple(opts.get("exclude_patterns", ()) or ()),
        max_depth=opts.get("max_depth"),
        excluded_suffixes=opts.get("excluded_suffixes", None),
        allowed_suffixes=tuple(opts.get("allowed_suffixes", ()) or ()),
    )


def _host_ok(host: str, base_host: str, allow_subdomains: bool) -> bool:
    return host == base_host or (allow_subdomains and host.endswith("." + base_host))


def _base_dir_segments(start_url: str) -> list[str]:
    parts = urlsplit(start_url)
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return []
    if parts.path.endswith("/"):
        return segments
    return segments[:-1]


def _depth_ok(start_url: str, url: str, max_depth: int) -> bool:
    base_segs = _base_dir_segments(start_url)
    try:
        url_segs = [s for s in urlsplit(url).path.split("/") if s]
    except ValueError:
        return False
    if len(url_segs) < len(base_segs):
        return False
    if url_segs[: len(base_segs)] != base_segs:
        return False
    return len(url_segs) - len(base_segs) <= max_depth
