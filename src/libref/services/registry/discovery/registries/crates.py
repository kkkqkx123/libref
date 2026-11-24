"""Discovery adapter for the crates registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from libref.crawler.registry import RegistryError
from libref.services.registry.discovery.models import DiscoveryCandidate
from libref.services.registry.discovery.ranking import _normalize_query, _popularity


class CratesSearch:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    async def _search_crates(
        self, client, query: str, include_prerelease: bool = False
    ) -> list[DiscoveryCandidate]:
        from libref.crawler.http import resilient_get
        from libref.crawler.registry import (
            fetch_snapshot,
            is_repo_host_url,
            registry_base_for,
            sanitize_http_url,
        )
        from libref.crawler.registry.crates import (
            crates_search_url,
            parse_crates_search,
        )

        try:
            base = registry_base_for("crates", self._settings)
        except ValueError:
            base = "https://crates.io"
        try:
            resp = await resilient_get(
                client,
                crates_search_url(base, query, 20),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            fallback = await self._snapshot_candidate(
                client, "crates", query, include_prerelease
            )
            if fallback is not None:
                return [fallback]
            raise RegistryError(
                "retryable", f"crates search unreachable: {exc}"
            ) from None
        if resp.status_code >= 400:
            fallback = await self._snapshot_candidate(
                client, "crates", query, include_prerelease
            )
            if fallback is not None:
                return [fallback]
            raise RegistryError("retryable", f"crates search HTTP {resp.status_code}")
        allow_private = self._allow_private()
        out: list[DiscoveryCandidate] = []
        norm = _normalize_query("crates", query)
        for item in parse_crates_search(resp.json()):
            raw_docs = sanitize_http_url(
                item.get("docs_url"), allow_private=allow_private
            )
            docs = [raw_docs] if raw_docs else []
            homepage = sanitize_http_url(item["homepage"], allow_private=allow_private)
            repo = sanitize_http_url(item["repo_url"], allow_private=allow_private)
            version = item["version"]
            source = "search:crates"
            if item["name"].lower() == norm:
                try:
                    snap = await fetch_snapshot(
                        "crates",
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
            if not docs and homepage and not is_repo_host_url(homepage):
                docs = [homepage]
            out.append(
                DiscoveryCandidate(
                    ecosystem="crates",
                    name=item["name"],
                    version=version,
                    description=item["description"],
                    docs_url=docs[0] if docs else None,
                    candidates=docs,
                    homepage=homepage,
                    repo_url=repo,
                    popularity=_popularity(item["popularity"]),
                    updated=str(item["updated"] or ""),
                    exact=item["name"].lower() == norm,
                    rank=item["position"],
                    source=source,
                )
            )
        return out
