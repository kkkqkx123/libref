"""Discovery adapter for the go registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from libref.crawler.registry import RegistryError
from libref.services.registry.discovery.models import DiscoveryCandidate
from libref.services.registry.discovery.ranking import _normalize_query


class GoSearch:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    async def _search_go(
        self, client, query: str, include_prerelease: bool = False
    ) -> list[DiscoveryCandidate]:
        from libref.crawler.http import resilient_get
        from libref.crawler.registry import fetch_snapshot, sanitize_http_url
        from libref.crawler.registry.go import go_search_url, parse_go_search_html

        try:
            resp = await resilient_get(
                client,
                go_search_url(query),
                headers={"Accept": "text/html, */*"},
            )
        except httpx.HTTPError as exc:
            fallback = await self._snapshot_candidate(
                client, "go", query, include_prerelease
            )
            if fallback is not None:
                return [fallback]
            raise RegistryError("retryable", f"go search unreachable: {exc}") from None
        if resp.status_code >= 400:
            fallback = await self._snapshot_candidate(
                client, "go", query, include_prerelease
            )
            if fallback is not None:
                return [fallback]
            raise RegistryError("retryable", f"go search HTTP {resp.status_code}")
        allow_private = self._allow_private()
        out: list[DiscoveryCandidate] = []
        norm = _normalize_query("go", query)
        for item in parse_go_search_html(resp.text, limit=20):
            docs: list[str] = []
            version = item["version"]
            source = "search:go"
            if item["name"] == norm:
                try:
                    snap = await fetch_snapshot(
                        "go",
                        item["name"],
                        client,
                        self._settings,
                        include_prerelease,
                    )
                except RegistryError:
                    snap = None
                if snap is not None:
                    docs = list(snap.doc_candidates)
                    version = snap.stable or version
                    source = "registry"
            if not docs:
                from libref.crawler.registry.go import go_docs_url

                clean = sanitize_http_url(
                    go_docs_url(item["name"]), allow_private=allow_private
                )
                docs = [clean] if clean else []
            out.append(
                DiscoveryCandidate(
                    ecosystem="go",
                    name=item["name"],
                    version=version,
                    description=item["description"],
                    docs_url=docs[0] if docs else None,
                    candidates=docs,
                    homepage=None,
                    repo_url=None,
                    popularity=0.0,
                    updated="",
                    exact=item["name"] == norm,
                    rank=item["position"],
                    source=source,
                )
            )
        if out and not any(c.exact for c in out):
            try:
                extra = await self._snapshot_candidate(
                    client, "go", query, include_prerelease
                )
            except RegistryError:
                extra = None
            if extra is not None and all(c.name != extra.name for c in out):
                out.append(extra)
        if not out:
            try:
                snap = await fetch_snapshot(
                    "go", query.strip(), client, self._settings, include_prerelease
                )
            except RegistryError as exc:
                if exc.kind == "not_found":
                    return []
                raise
            docs = list(snap.doc_candidates)
            out.append(
                DiscoveryCandidate(
                    ecosystem="go",
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
            )
        return out
