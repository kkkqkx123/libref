"""Discovery adapter for the nuget registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from libref.crawler.registry import RegistryError
from libref.services.registry.discovery.models import DiscoveryCandidate
from libref.services.registry.discovery.ranking import _normalize_query, _popularity


class NugetSearch:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    async def _search_nuget(
        self, client, query: str, include_prerelease: bool = False
    ) -> list[DiscoveryCandidate]:
        from libref.crawler.http import resilient_get
        from libref.crawler.registry import (
            fetch_snapshot,
            is_repo_host_url,
            sanitize_http_url,
        )
        from libref.crawler.registry.nuget import nuget_search_url, parse_nuget_search

        try:
            resp = await resilient_get(
                client,
                nuget_search_url(query, 20),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            fallback = await self._snapshot_candidate(
                client, "nuget", query, include_prerelease
            )
            if fallback is not None:
                return [fallback]
            raise RegistryError(
                "retryable", f"nuget search unreachable: {exc}"
            ) from None
        if resp.status_code >= 400:
            fallback = await self._snapshot_candidate(
                client, "nuget", query, include_prerelease
            )
            if fallback is not None:
                return [fallback]
            raise RegistryError("retryable", f"nuget search HTTP {resp.status_code}")
        allow_private = self._allow_private()
        out: list[DiscoveryCandidate] = []
        norm = _normalize_query("nuget", query)
        for item in parse_nuget_search(resp.json()):
            homepage = sanitize_http_url(item["homepage"], allow_private=allow_private)
            docs = [homepage] if homepage and not is_repo_host_url(homepage) else []
            version = item["version"]
            source = "search:nuget"
            if item["name"].lower() == norm:
                try:
                    snap = await fetch_snapshot(
                        "nuget",
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
                    version = snap.stable or version
                    source = "registry"
            out.append(
                DiscoveryCandidate(
                    ecosystem="nuget",
                    name=item["name"],
                    version=version,
                    description=item["description"],
                    docs_url=docs[0] if docs else None,
                    candidates=docs,
                    homepage=homepage,
                    repo_url=None,
                    popularity=_popularity(item["popularity"]),
                    updated="",
                    exact=item["name"].lower() == norm,
                    rank=item["position"],
                    source=source,
                )
            )
        return out
