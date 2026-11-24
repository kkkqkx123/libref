"""Discovery adapter for the npm registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from libref.crawler.registry import RegistryError
from libref.services.registry.discovery.models import DiscoveryCandidate
from libref.services.registry.discovery.ranking import _normalize_query, _popularity


class NpmSearch:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    async def _search_npm(
        self, client, query: str, include_prerelease: bool = False
    ) -> list[DiscoveryCandidate]:
        from libref.crawler.http import resilient_get
        from libref.crawler.registry import (
            fetch_snapshot,
            is_repo_host_url,
            sanitize_http_url,
        )
        from libref.crawler.registry.npm import npm_search_url_for, parse_npm_search

        try:
            resp = await resilient_get(
                client,
                npm_search_url_for(self._settings, query, 20),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            fallback = await self._snapshot_candidate(
                client, "npm", query, include_prerelease
            )
            if fallback is not None:
                return [fallback]
            raise RegistryError("retryable", f"npm search unreachable: {exc}") from None
        if resp.status_code >= 400:
            fallback = await self._snapshot_candidate(
                client, "npm", query, include_prerelease
            )
            if fallback is not None:
                return [fallback]
            raise RegistryError("retryable", f"npm search HTTP {resp.status_code}")
        allow_private = self._allow_private()
        out: list[DiscoveryCandidate] = []
        norm = _normalize_query("npm", query)
        for item in parse_npm_search(resp.json()):
            if item["name"].lower() == norm:
                try:
                    snap = await fetch_snapshot(
                        "npm",
                        item["name"],
                        client,
                        self._settings,
                        include_prerelease,
                    )
                except RegistryError:
                    snap = None
                if snap is not None:
                    docs = list(snap.doc_candidates)
                    out.append(
                        DiscoveryCandidate(
                            ecosystem="npm",
                            name=snap.name,
                            version=snap.stable or item["version"],
                            description=item["description"],
                            docs_url=docs[0] if docs else None,
                            candidates=docs,
                            homepage=snap.homepage,
                            repo_url=snap.repo_url,
                            popularity=_popularity(item["popularity"]),
                            updated=str(item["updated"] or ""),
                            exact=True,
                            rank=item["position"],
                            source="registry",
                        )
                    )
                    continue
            homepage = sanitize_http_url(item["homepage"], allow_private=allow_private)
            repo = sanitize_http_url(item["repo_url"], allow_private=allow_private)
            docs = [homepage] if homepage and not is_repo_host_url(homepage) else []
            out.append(
                DiscoveryCandidate(
                    ecosystem="npm",
                    name=item["name"],
                    version=item["version"],
                    description=item["description"],
                    docs_url=docs[0] if docs else None,
                    candidates=docs,
                    homepage=homepage,
                    repo_url=repo,
                    popularity=_popularity(item["popularity"]),
                    updated=str(item["updated"] or ""),
                    exact=item["name"].lower() == norm,
                    rank=item["position"],
                    source="search:npm",
                )
            )
        return out
