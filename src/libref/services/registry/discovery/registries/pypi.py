"""Discovery adapter for the pypi registry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from libref.crawler.registry import RegistryError
from libref.services.registry.discovery.models import DiscoveryCandidate
from libref.services.registry.discovery.ranking import _normalize_query


class PypiSearch:
    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    async def _search_pypi(
        self, client, query: str, include_prerelease: bool = False
    ) -> list[DiscoveryCandidate]:
        from libref.crawler.http import resilient_get
        from libref.crawler.registry import fetch_snapshot
        from libref.crawler.registry.pypi import (
            parse_pypi_search_html,
            pypi_search_url,
        )

        async def _exact_snapshot(name: str):
            try:
                return await fetch_snapshot(
                    "pypi", name, client, self._settings, include_prerelease
                )
            except RegistryError as exc:
                if exc.kind == "not_found":
                    return None
                raise

        items: list[dict[str, str]] = []
        try:
            resp = await resilient_get(
                client,
                pypi_search_url(query),
                headers={"Accept": "text/html, */*"},
            )
        except httpx.HTTPError as exc:
            raise RegistryError(
                "retryable", f"pypi search unreachable: {exc}"
            ) from None
        if resp.status_code == 200:
            items = parse_pypi_search_html(resp.text, limit=20)
        search_degraded = resp.status_code != 200
        if not items:
            snap = await _exact_snapshot(query.strip())
            if snap is None:
                if search_degraded:
                    raise RegistryError(
                        "retryable", f"pypi search HTTP {resp.status_code}"
                    )
                return []
            docs = list(snap.doc_candidates)
            return [
                DiscoveryCandidate(
                    ecosystem="pypi",
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
            ]
        norm = _normalize_query("pypi", query)
        out: list[DiscoveryCandidate] = []
        for position, item in enumerate(items):
            if item["name"].lower() == norm:
                snap = await _exact_snapshot(item["name"])
                if snap is not None:
                    docs = list(snap.doc_candidates)
                    out.append(
                        DiscoveryCandidate(
                            ecosystem="pypi",
                            name=snap.name,
                            version=snap.stable or item["version"],
                            description=item["description"],
                            docs_url=docs[0] if docs else None,
                            candidates=docs,
                            homepage=snap.homepage,
                            repo_url=snap.repo_url,
                            popularity=0.0,
                            updated="",
                            exact=True,
                            rank=position,
                            source="registry",
                        )
                    )
                    continue
            out.append(
                DiscoveryCandidate(
                    ecosystem="pypi",
                    name=item["name"],
                    version=item["version"],
                    description=item["description"],
                    popularity=0.0,
                    updated="",
                    exact=item["name"].lower() == norm,
                    rank=position,
                    source="search:pypi",
                )
            )
        return out
