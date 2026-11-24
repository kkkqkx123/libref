"""General web search discovery source."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from libref.crawler.registry import RegistryError
from libref.services.registry.discovery.models import DiscoveryCandidate
from libref.services.registry.discovery.ranking import _annotate
from libref.services.registry.resolver import is_fresh


class WebSearch:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def search_web(
        self,
        query: str,
        limit: int | None = None,
    ) -> list[dict]:
        return asyncio.run(self.search_web_async(query, limit))

    async def search_web_async(
        self,
        query: str,
        limit: int | None = None,
        client=None,
    ) -> list[dict]:
        """Explicit web search; standalone entry outside the registry fan-out."""
        from libref.config import setting_or_default
        from libref.crawler.http import build_async_client

        if not query.strip():
            raise ValueError("empty discovery query")
        if not bool(setting_or_default(self._settings, "discovery_enabled")):
            raise ValueError("library discovery is disabled")
        topn = (
            limit
            if limit is not None
            else int(setting_or_default(self._settings, "discovery_topn"))
        )
        if topn < 1 or topn > 100:
            raise ValueError("limit must be between 1 and 100")
        if client is None:
            async with build_async_client(self._settings) as owned:
                return await self._search_web_with(owned, query, topn)
        return await self._search_web_with(client, query, topn)

    async def _search_web_with(self, client, query: str, topn: int) -> list[dict]:
        from libref.config import setting_or_default
        from libref.crawler.http import resilient_get
        from libref.crawler.registry import sanitize_http_url
        from libref.crawler.websearch import (
            WEB_SEARCH_USER_AGENT,
            candidate_name,
            classify_target,
            parse_ddg_html,
            web_search_url,
        )

        cached = self._read_cache("web", query)
        ttl = float(setting_or_default(self._settings, "discovery_cache_ttl_days"))
        offline = bool(setting_or_default(self._settings, "discovery_offline"))
        if cached is not None and is_fresh(cached["fetched_at"], ttl):
            return _annotate(
                [
                    DiscoveryCandidate(**c).model_dump()
                    for c in cached["candidates"][:topn]
                ]
            )
        if offline:
            if cached is None:
                raise RegistryError("retryable", "offline cache miss")
            return _annotate(
                [
                    DiscoveryCandidate(**c).model_dump()
                    for c in cached["candidates"][:topn]
                ]
            )
        try:
            resp = await resilient_get(
                client,
                web_search_url(query, topn),
                headers={
                    "Accept": "text/html, */*",
                    "User-Agent": WEB_SEARCH_USER_AGENT,
                },
            )
        except httpx.HTTPError as exc:
            raise RegistryError("retryable", f"web search unreachable: {exc}") from None
        if resp.status_code != 200:
            raise RegistryError("retryable", f"web search HTTP {resp.status_code}")
        out: list[DiscoveryCandidate] = []
        for item in parse_ddg_html(resp.text, limit=topn):
            clean = sanitize_http_url(item["url"])
            if clean is None:
                continue
            kind = classify_target(clean)
            docs = [clean] if kind == "docs" else []
            out.append(
                DiscoveryCandidate(
                    ecosystem="web",
                    name=candidate_name(clean, item["title"]),
                    version="",
                    description=item["snippet"],
                    docs_url=docs[0] if docs else None,
                    candidates=docs,
                    homepage=clean if kind == "docs" else None,
                    repo_url=clean if kind == "repo" else None,
                    popularity=0.0,
                    updated="",
                    exact=False,
                    rank=item["position"],
                    source="search:web",
                )
            )
        self._write_cache("web", query, [c.model_dump() for c in out])
        return _annotate([c.model_dump() for c in out])
