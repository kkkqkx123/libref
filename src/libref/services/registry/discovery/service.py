"""Library discovery service composition root."""

from __future__ import annotations

import asyncio

from libref.crawler.registry import SUPPORTED_ECOSYSTEMS, RegistryError
from libref.db.contract import MetadataSession
from libref.services.registry.discovery.cache import Cache
from libref.services.registry.discovery.github_search import GithubSearch
from libref.services.registry.discovery.models import DiscoveryCandidate
from libref.services.registry.discovery.ranking import (
    _annotate,
    rank_candidates,
)
from libref.services.registry.discovery.registries.crates import CratesSearch
from libref.services.registry.discovery.registries.go import GoSearch
from libref.services.registry.discovery.registries.maven import MavenSearch
from libref.services.registry.discovery.registries.npm import NpmSearch
from libref.services.registry.discovery.registries.nuget import NugetSearch
from libref.services.registry.discovery.registries.pypi import PypiSearch
from libref.services.registry.discovery.web_search import WebSearch
from libref.services.registry.resolver import is_fresh

__all__ = ["DiscoveryService"]


class DiscoveryService(
    PypiSearch,
    NpmSearch,
    MavenSearch,
    GoSearch,
    CratesSearch,
    NugetSearch,
    WebSearch,
    GithubSearch,
    Cache,
):
    def __init__(self, conn: MetadataSession, settings=None) -> None:
        self._conn = conn
        self._settings = settings

    def search(
        self,
        query: str,
        ecosystem: str | None = None,
        limit: int | None = None,
        include_prerelease: bool = False,
    ) -> list[dict]:
        return asyncio.run(
            self.search_async(
                query, ecosystem, limit, include_prerelease=include_prerelease
            )
        )

    async def search_async(
        self,
        query: str,
        ecosystem: str | None = None,
        limit: int | None = None,
        client=None,
        include_prerelease: bool = False,
    ) -> list[dict]:
        from libref.config import setting_or_default
        from libref.crawler.http import build_async_client
        from libref.crawler.registry import canonical_ecosystem

        if not query.strip():
            raise ValueError("empty discovery query")
        eco = canonical_ecosystem(ecosystem) if ecosystem is not None else None
        if ecosystem is not None and eco not in SUPPORTED_ECOSYSTEMS:
            raise ValueError(f"unsupported ecosystem: {ecosystem}")
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
                return await self._search_with(
                    owned, query, eco, topn, include_prerelease
                )
        return await self._search_with(client, query, eco, topn, include_prerelease)

    async def _search_with(
        self,
        client,
        query: str,
        ecosystem: str | None,
        topn: int,
        include_prerelease: bool = False,
    ) -> list[dict]:
        from libref.config import setting_or_default

        scope = (ecosystem or "*") + ("+pre" if include_prerelease else "")
        cached = self._read_cache(scope, query)
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
        ecos = (
            [ecosystem]
            if ecosystem
            else ["npm", "maven", "pypi", "go", "crates", "nuget"]
        )
        gathered: list[DiscoveryCandidate] = []
        failures: list[str] = []
        for eco in ecos:
            try:
                gathered.extend(
                    await self._search_one(client, eco, query, include_prerelease)
                )
            except RegistryError as exc:
                failures.append(f"{eco}: {exc}")
        if self._github_eligible(ecosystem):
            try:
                gathered.extend(await self._search_github(client, query))
            except RegistryError as exc:
                failures.append(f"github: {exc}")
        if not gathered and failures:
            raise RegistryError(
                "retryable",
                "all discovery sources unreachable (" + "; ".join(failures) + ")",
            )
        ranked = rank_candidates(gathered, query)
        self._write_cache(scope, query, [c.model_dump() for c in ranked])
        return _annotate([c.model_dump() for c in ranked[:topn]])

    def _allow_private(self) -> bool:
        from libref.config import setting_or_default

        return bool(setting_or_default(self._settings, "registry_allow_private"))

    async def _snapshot_candidate(
        self, client, ecosystem: str, query: str, include_prerelease: bool = False
    ) -> DiscoveryCandidate | None:
        """Exact-name registry enrichment for degraded search: snapshot hit only, miss stays silent."""
        from libref.crawler.registry import fetch_snapshot

        try:
            snap = await fetch_snapshot(
                ecosystem, query.strip(), client, self._settings, include_prerelease
            )
        except RegistryError as exc:
            if exc.kind == "not_found":
                return None
            raise
        docs = list(snap.doc_candidates)
        return DiscoveryCandidate(
            ecosystem=snap.ecosystem,
            name=snap.name,
            version=snap.stable or "",
            description="",
            docs_url=docs[0] if docs else None,
            candidates=docs,
            homepage=snap.homepage,
            repo_url=snap.repo_url,
            popularity=0.0,
            updated="",
            exact=True,
            source="registry",
        )

    async def _search_one(
        self, client, ecosystem: str, query: str, include_prerelease: bool = False
    ) -> list[DiscoveryCandidate]:
        from libref.crawler.registry import canonical_ecosystem

        eco = canonical_ecosystem(ecosystem) or ecosystem
        if eco == "pypi":
            return await self._search_pypi(client, query, include_prerelease)
        if eco == "npm":
            return await self._search_npm(client, query, include_prerelease)
        if eco == "maven":
            return await self._search_maven(client, query, include_prerelease)
        if eco == "go":
            return await self._search_go(client, query, include_prerelease)
        if eco == "crates":
            return await self._search_crates(client, query, include_prerelease)
        if eco == "nuget":
            return await self._search_nuget(client, query, include_prerelease)
        raise ValueError(f"unsupported ecosystem: {ecosystem}")
