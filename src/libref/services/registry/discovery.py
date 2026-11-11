"""Library discovery orchestration: fuzzy name search across registries plus deterministic ranking.

Read path with a rebuildable cache: cache hit within TTL returns without network;
offline mode never touches the network. Ranking is a fixed lexicographic key
(docs availability, exact match, native order, popularity, freshness) with no model involved.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import httpx
from pydantic import BaseModel, Field

from libref.crawler.registry import SUPPORTED_ECOSYSTEMS, RegistryError
from libref.services.registry.resolver import is_fresh


class DiscoveryCandidate(BaseModel):
    ecosystem: str
    name: str
    version: str = ""
    description: str = ""
    docs_url: str | None = None
    candidates: list[str] = Field(default_factory=list)
    homepage: str | None = None
    repo_url: str | None = None
    popularity: float = 0.0
    updated: str = ""
    exact: bool = False
    rank: int = 0
    source: str = "search"


def _normalize_query(ecosystem: str, query: str) -> str:
    text = query.strip()
    from libref.crawler.registry import canonical_ecosystem

    eco = canonical_ecosystem(ecosystem) or ecosystem.strip().lower()
    if eco == "maven":
        from libref.crawler.registry.maven import normalize_maven_name

        try:
            return normalize_maven_name(text)
        except ValueError:
            return text
    if eco == "pypi":
        from libref.crawler.registry.pypi import normalize_pypi_name

        return normalize_pypi_name(text)
    if eco == "npm":
        from libref.crawler.registry.npm import normalize_npm_name

        return normalize_npm_name(text)
    if eco == "go":
        from libref.crawler.registry.go import normalize_go_name

        try:
            return normalize_go_name(text)
        except ValueError:
            return text
    if eco == "crates":
        from libref.crawler.registry.crates import normalize_crates_name

        try:
            return normalize_crates_name(text)
        except ValueError:
            return text.lower()
    if eco == "nuget":
        from libref.crawler.registry.nuget import normalize_nuget_name

        try:
            return normalize_nuget_name(text)
        except ValueError:
            return text.lower()
    return text.lower()


_REPOLESS_NOTE = (
    "no repository address; resolve with target=repo"
    " or pass an explicit source before code indexing"
)


def _annotate(rows: list[dict]) -> list[dict]:
    for row in rows:
        if not row.get("repo_url"):
            row["note"] = _REPOLESS_NOTE
    return rows


def _popularity(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return 0.0


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


def _source_tier(source: str) -> int:
    return 0 if source == "registry" else 1


def rank_candidates(
    items: list[DiscoveryCandidate], query: str
) -> list[DiscoveryCandidate]:
    """Deterministic order: registry source first, then docs, exact match, native order, popularity, freshness."""
    norm = query.strip().lower()
    return sorted(
        items,
        key=lambda c: (
            _source_tier(c.source),
            0 if (c.docs_url or c.candidates) else 1,
            0 if c.name.lower() == norm else 1,
            c.rank,
            -_popularity(c.popularity),
            c.updated or "",
        ),
    )


class DiscoveryService:
    """Fuzzy library search with cache; the only reader/writer of discovery_cache."""

    def __init__(self, conn: sqlite3.Connection, settings=None) -> None:
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
        failures = 0
        for eco in ecos:
            try:
                gathered.extend(
                    await self._search_one(client, eco, query, include_prerelease)
                )
            except RegistryError:
                failures += 1
        if self._github_eligible(ecosystem):
            try:
                gathered.extend(await self._search_github(client, query))
            except RegistryError:
                failures += 1
        if not gathered and failures:
            raise RegistryError("retryable", "all discovery sources unreachable")
        ranked = rank_candidates(gathered, query)
        self._write_cache(scope, query, [c.model_dump() for c in ranked])
        return _annotate([c.model_dump() for c in ranked[:topn]])

    def _github_eligible(self, ecosystem: str | None) -> bool:
        from libref.config import setting_or_default

        if ecosystem is not None:
            return False
        token = setting_or_default(self._settings, "discovery_github_token")
        return bool(token and str(token).strip())

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
        search_degraded = resp.status_code == 429 or resp.status_code >= 500
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

    async def _search_npm(
        self, client, query: str, include_prerelease: bool = False
    ) -> list[DiscoveryCandidate]:
        from libref.crawler.http import resilient_get
        from libref.crawler.registry import fetch_snapshot, is_repo_host_url
        from libref.crawler.registry.npm import npm_search_url_for, parse_npm_search

        try:
            resp = await resilient_get(
                client,
                npm_search_url_for(self._settings, query, 20),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise RegistryError("retryable", f"npm search unreachable: {exc}") from None
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"npm search HTTP {resp.status_code}")
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
            homepage = item["homepage"]
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
                    repo_url=item["repo_url"],
                    popularity=_popularity(item["popularity"]),
                    updated=str(item["updated"] or ""),
                    exact=item["name"].lower() == norm,
                    rank=item["position"],
                    source="search:npm",
                )
            )
        return out

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
            raise RegistryError(
                "retryable", f"maven search unreachable: {exc}"
            ) from None
        if resp.status_code >= 400:
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

    async def _search_go(
        self, client, query: str, include_prerelease: bool = False
    ) -> list[DiscoveryCandidate]:
        from libref.crawler.http import resilient_get
        from libref.crawler.registry import fetch_snapshot
        from libref.crawler.registry.go import go_search_url, parse_go_search_html

        try:
            resp = await resilient_get(
                client,
                go_search_url(query),
                headers={"Accept": "text/html, */*"},
            )
        except httpx.HTTPError as exc:
            raise RegistryError("retryable", f"go search unreachable: {exc}") from None
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"go search HTTP {resp.status_code}")
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

                docs = [go_docs_url(item["name"])]
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

    async def _search_crates(
        self, client, query: str, include_prerelease: bool = False
    ) -> list[DiscoveryCandidate]:
        from libref.crawler.http import resilient_get
        from libref.crawler.registry import (
            fetch_snapshot,
            is_repo_host_url,
            registry_base_for,
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
            raise RegistryError(
                "retryable", f"crates search unreachable: {exc}"
            ) from None
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"crates search HTTP {resp.status_code}")
        out: list[DiscoveryCandidate] = []
        norm = _normalize_query("crates", query)
        for item in parse_crates_search(resp.json()):
            docs = [item["docs_url"]] if item.get("docs_url") else []
            homepage = item["homepage"]
            repo = item["repo_url"]
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

    async def _search_nuget(
        self, client, query: str, include_prerelease: bool = False
    ) -> list[DiscoveryCandidate]:
        from libref.crawler.http import resilient_get
        from libref.crawler.registry import fetch_snapshot, is_repo_host_url
        from libref.crawler.registry.nuget import nuget_search_url, parse_nuget_search

        try:
            resp = await resilient_get(
                client,
                nuget_search_url(query, 20),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise RegistryError(
                "retryable", f"nuget search unreachable: {exc}"
            ) from None
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"nuget search HTTP {resp.status_code}")
        out: list[DiscoveryCandidate] = []
        norm = _normalize_query("nuget", query)
        for item in parse_nuget_search(resp.json()):
            homepage = item["homepage"]
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

    def _read_cache(self, ecosystem: str, query: str) -> dict | None:
        row = self._conn.execute(
            "SELECT candidates, fetched_at FROM discovery_cache WHERE ecosystem=? AND query=?",
            (ecosystem, query.strip().lower()),
        ).fetchone()
        if row is None:
            return None
        try:
            candidates = json.loads(row[0]) if row[0] else []
        except ValueError:
            return None
        return {"candidates": candidates, "fetched_at": row[1]}

    def _write_cache(self, ecosystem: str, query: str, candidates: list[dict]) -> None:
        from libref.db.conn import write_lock

        with write_lock(self._conn):
            self._conn.execute(
                "INSERT INTO discovery_cache(ecosystem, query, candidates, fetched_at) VALUES(?,?,?,?) "
                "ON CONFLICT(ecosystem, query) DO UPDATE SET candidates=excluded.candidates, "
                "fetched_at=excluded.fetched_at",
                (
                    ecosystem,
                    query.strip().lower(),
                    json.dumps(candidates, ensure_ascii=False),
                    time.time(),
                ),
            )
            self._conn.commit()
