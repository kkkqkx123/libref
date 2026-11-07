"""URL 链式发现：robots→llms.txt→sitemap(.gz/index)（蓝本 crawl4ai AsyncUrlSeeder 模式）。

定制点：所有请求经注入的 httpx.AsyncClient（测试用 MockTransport，不联网）；
llms.txt 优先于 sitemap；同 host 限定 + robots Disallow 过滤 + 去重；
HEAD 存活预筛为可选步骤。
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from libref.crawler.robots import RobotsChecker

_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


@dataclass
class SeedResult:
    urls: list[str] = field(default_factory=list)
    source: str = "none"  # llms.txt / sitemap / none


class UrlSeeder:
    def __init__(self, client: httpx.AsyncClient, robots: RobotsChecker) -> None:
        self._client = client
        self._robots = robots

    async def discover(
        self,
        start_url: str,
        *,
        prefilter: bool = False,
        max_pages: int | None = None,
    ) -> SeedResult:
        """从站点根发现文档 URL；start_url 任意路径，取 scheme+netloc 为站点根。"""
        parts = urlsplit(start_url)
        base = f"{parts.scheme}://{parts.netloc}"

        result = await self._from_llms_txt(base)
        if result.source == "none":
            result = await self._from_sitemap(base)

        seen: set[str] = set()
        urls: list[str] = []
        for url in result.urls:
            if url in seen or urlsplit(url).netloc != parts.netloc:
                continue
            if not await self._robots.can_fetch(url):
                continue
            if prefilter and not await self._head_ok(url):
                continue
            seen.add(url)
            urls.append(url)
            if max_pages is not None and len(urls) >= max_pages:
                break
        return SeedResult(urls=urls, source=result.source)

    # ── llms.txt ──

    async def _from_llms_txt(self, base: str) -> SeedResult:
        try:
            resp = await self._client.get(f"{base}/llms.txt")
        except httpx.HTTPError:
            return SeedResult()
        if resp.status_code >= 400:
            return SeedResult()
        urls = []
        for line in resp.text.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            # llms.txt 规范用 Markdown 链接 [text](url)；也容忍裸 URL / 相对路径
            for token in _LINK_RE.findall(line) + line[2:].split():
                if token.startswith(("http://", "https://")):
                    urls.append(token)
                elif token.startswith("/"):
                    urls.append(urljoin(base, token))
        if not urls:
            return SeedResult()
        return SeedResult(urls=urls, source="llms.txt")

    # ── sitemap（含 .gz 与 index 递归一层）──

    async def _from_sitemap(self, base: str, depth: int = 0) -> SeedResult:
        for candidate in (f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"):
            body = await self._fetch_body(candidate)
            if body is None:
                continue
            if candidate.endswith(".gz") or body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            text = body.decode("utf-8", errors="replace")
            if "<sitemapindex" in text and depth == 0:
                # sitemap index：先于 <loc> 泛解析判定，递归一层取子 sitemap 的 URL
                nested: list[str] = []
                for child in self._parse_sitemap_locs(text):
                    child_body = await self._fetch_body(
                        child if child.startswith("http") else urljoin(base, child)
                    )
                    if child_body is None:
                        continue
                    if child_body[:2] == b"\x1f\x8b":
                        child_body = gzip.decompress(child_body)
                    nested.extend(
                        self._parse_loc_tags(child_body.decode("utf-8", errors="replace"))
                    )
                urls = nested
            else:
                urls = self._parse_loc_tags(text)
            if urls:
                return SeedResult(urls=urls, source="sitemap")
        return SeedResult()

    async def _fetch_body(self, url: str) -> bytes | None:
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None
        return resp.content

    async def _head_ok(self, url: str) -> bool:
        try:
            resp = await self._client.head(url, follow_redirects=True)
        except httpx.HTTPError:
            return False
        if resp.status_code >= 400:
            return False
        ctype = resp.headers.get("content-type", "")
        allowed = ("text/html", "text/plain", "application/xhtml", "text/markdown")
        return any(a in ctype for a in allowed)

    @staticmethod
    def _parse_loc_tags(xml_text: str) -> list[str]:
        """容错解析 <loc> 标签（不引入 XML 依赖，sitemap 结构固定）。"""
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
        """sitemapindex 中 <sitemap><loc> 子 sitemap 列表。"""
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
