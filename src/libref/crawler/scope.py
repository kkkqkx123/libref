"""抓取范围策略：起始地址与用户选项决定地址去留。纯逻辑、零网络。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class ScopePolicy:
    """输入起始地址与显式选项，输出是否接受候选地址。

    默认与历史行为一致：仅同站接受、跨站拒绝；显式选项仅在给出时生效。
    """

    start_url: str
    allow_cross_site: bool = False
    allow_subdomains: bool = False
    path_prefixes: tuple[str, ...] = ()
    path_suffixes: tuple[str, ...] = ()
    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    max_depth: int | None = None
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
            self, "_include_res",
            tuple(re.compile(p) for p in (include_patterns or ())),
        )
        object.__setattr__(
            self, "_exclude_res",
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
            parts.netloc.lower(), base.netloc.lower(), self.allow_subdomains,
        ):
            return False
        path = parts.path or "/"
        if self.path_prefixes and not any(path.startswith(p) for p in self.path_prefixes):
            return False
        if self.path_suffixes and not any(path.endswith(s) for s in self.path_suffixes):
            return False
        for rx in self._exclude_res:
            if rx.search(url):
                return False
        if self._include_res and not any(rx.search(url) for rx in self._include_res):
            return False
        if self.max_depth is not None and not _depth_ok(self.start_url, url, self.max_depth):
            return False
        return True

    def filter(self, urls: list[str]) -> tuple[list[str], list[str]]:
        accepted: list[str] = []
        rejected: list[str] = []
        for url in urls:
            (accepted if self.accept(url) else rejected).append(url)
        return accepted, rejected


def build_scope(start_url: str, opts: dict | None) -> ScopePolicy:
    """按任务参数字典构造策略；空选项即默认同站策略。"""
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
    )


def _host_ok(host: str, base_host: str, allow_subdomains: bool) -> bool:
    if host == base_host:
        return True
    if allow_subdomains and host.endswith("." + base_host):
        return True
    return False


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
