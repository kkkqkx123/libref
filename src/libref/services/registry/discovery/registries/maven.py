"""Discovery adapter for the maven registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from libref.crawler.registry import RegistryError
from libref.services.registry.discovery.models import DiscoveryCandidate
from libref.services.registry.discovery.ranking import _normalize_query, _popularity


class MavenSearch:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    async def _search_maven(
        self, client, query: str, include_prerelease: bool = False
    ) -> list[DiscoveryCandidate]:
        from libref.crawler.http import resilient_get
        from libref.crawler.registry import fetch_snapshot
        from libref.crawler.registry.maven import maven_search_url, parse_maven_search

        try:
            resp = await resilient_get(
                client,
                maven_search_url(query, 20),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            fallback = await self._snapshot_candidate(
                client, "maven", query, include_prerelease
            )
            if fallback is not None:
                return [fallback]
            raise RegistryError(
                "retryable", f"maven search unreachable: {exc}"
            ) from None
        if resp.status_code >= 400:
            fallback = await self._snapshot_candidate(
                client, "maven", query, include_prerelease
            )
            if fallback is not None:
                return [fallback]
            raise RegistryError("retryable", f"maven search HTTP {resp.status_code}")
        out: list[DiscoveryCandidate] = []
        norm = _normalize_query("maven", query)
        for item in parse_maven_search(resp.json()):
            docs: list[str] = []
            homepage = None
            repo = None
            version = item["version"]
            source = "search:maven"
            if item["name"] == norm:
                try:
                    snap = await fetch_snapshot(
                        "maven",
                        item["name"],
                        client,
                        self._settings,
                        include_prerelease,
                    )
                except RegistryError:
                    snap = None
                if snap is not None:
                    docs = list(snap.doc_candidates)
                    homepage = snap.homepage
                    repo = snap.repo_url
                    version = snap.stable or version
                    source = "registry"
            out.append(
                DiscoveryCandidate(
                    ecosystem="maven",
                    name=item["name"],
                    version=version,
                    description="",
                    docs_url=docs[0] if docs else None,
                    candidates=docs,
                    homepage=homepage,
                    repo_url=repo,
                    popularity=_popularity(item["popularity"]),
                    updated=str(item["updated"] or ""),
                    exact=item["name"] == norm,
                    rank=item["position"],
                    source=source,
                )
            )
        return out
