"""GitHub repository search adapter."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from libref.crawler.registry import RegistryError
from libref.services.registry.discovery.models import DiscoveryCandidate
from libref.services.registry.discovery.ranking import _annotate, _popularity
from libref.services.registry.resolver import is_fresh


class GithubSearch:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def search_github(
        self,
        query: str,
        limit: int | None = None,
    ) -> list[dict]:
        return asyncio.run(self.search_github_async(query, limit))

    async def search_github_async(
        self,
        query: str,
        limit: int | None = None,
        client=None,
    ) -> list[dict]:
        """Explicit hosted search; standalone entry outside the registry fan-out."""
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
                return await self._search_github_with(owned, query, topn)
        return await self._search_github_with(client, query, topn)

    async def _search_github_with(self, client, query: str, topn: int) -> list[dict]:
        from libref.config import setting_or_default
        from libref.crawler.github import (
            github_search_headers,
            github_search_url,
            parse_github_search,
        )
        from libref.crawler.http import resilient_get

        cached = self._read_cache("github", query)
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
        token = str(setting_or_default(self._settings, "discovery_github_token") or "")
        try:
            resp = await resilient_get(
                client,
                github_search_url(f"{query.strip()} in:name", topn),
                headers=github_search_headers(token),
            )
        except httpx.HTTPError as exc:
            raise RegistryError(
                "retryable", f"github search unreachable: {exc}"
            ) from None
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"github search HTTP {resp.status_code}")
        out = _map_github_items(parse_github_search(resp.json()))
        self._write_cache("github", query, [c.model_dump() for c in out])
        return _annotate([c.model_dump() for c in out[:topn]])

    def _github_eligible(self, ecosystem: str | None) -> bool:
        from libref.config import setting_or_default

        if ecosystem is not None:
            return False
        token = setting_or_default(self._settings, "discovery_github_token")
        return bool(token and str(token).strip())

    async def _search_github(self, client, query: str) -> list[DiscoveryCandidate]:
        from libref.config import setting_or_default
        from libref.crawler.github import (
            github_search_headers,
            github_search_url,
            parse_github_search,
        )

        token = str(setting_or_default(self._settings, "discovery_github_token") or "")
        from libref.crawler.http import resilient_get

        try:
            resp = await resilient_get(
                client,
                github_search_url(f"{query.strip()} in:name", 10),
                headers=github_search_headers(token),
            )
        except httpx.HTTPError as exc:
            raise RegistryError(
                "retryable", f"github search unreachable: {exc}"
            ) from None
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"github search HTTP {resp.status_code}")
        return _map_github_items(parse_github_search(resp.json()))


def _map_github_items(items: list[dict]) -> list[DiscoveryCandidate]:
    return [
        DiscoveryCandidate(
            ecosystem="github",
            name=item["name"],
            version=item["version"],
            description=item["description"],
            docs_url=item["homepage"],
            candidates=[item["homepage"]] if item["homepage"] else [],
            homepage=item["homepage"],
            repo_url=item["repo_url"],
            popularity=_popularity(item["popularity"]),
            updated=str(item["updated"] or ""),
            exact=False,
            rank=item["position"],
            source="search:github",
        )
        for item in items
    ]
