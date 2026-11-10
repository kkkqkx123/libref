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
from libref.services.resolver import is_fresh


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
    if ecosystem == "maven":
        from libref.crawler.registry.maven import normalize_maven_name

        try:
            return normalize_maven_name(text)
        except ValueError:
            return text
    if ecosystem == "pypi":
        from libref.crawler.registry.pypi import normalize_pypi_name

        return normalize_pypi_name(text)
    if ecosystem == "npm":
        from libref.crawler.registry.npm import normalize_npm_name

        return normalize_npm_name(text)
    return text.lower()


def _popularity(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return 0.0


def rank_candidates(
    items: list[DiscoveryCandidate], query: str
) -> list[DiscoveryCandidate]:
    """Deterministic order: docs first, exact match next, native order, popularity, freshness last."""
    norm = query.strip().lower()
    return sorted(
        items,
        key=lambda c: (
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
    ) -> list[dict]:
        return asyncio.run(self.search_async(query, ecosystem, limit))

    async def search_async(
        self,
        query: str,
        ecosystem: str | None = None,
        limit: int | None = None,
        client=None,
    ) -> list[dict]:
        from libref.config import setting_or_default
        from libref.crawler.http import build_async_client

        if not query.strip():
            raise ValueError("empty discovery query")
        if ecosystem is not None and ecosystem not in SUPPORTED_ECOSYSTEMS:
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
                return await self._search_with(owned, query, ecosystem, topn)
        return await self._search_with(client, query, ecosystem, topn)

    async def _search_with(
        self, client, query: str, ecosystem: str | None, topn: int
    ) -> list[dict]:
        from libref.config import setting_or_default

        scope = ecosystem or "*"
        cached = self._read_cache(scope, query)
        ttl = float(setting_or_default(self._settings, "discovery_cache_ttl_days"))
        offline = bool(setting_or_default(self._settings, "discovery_offline"))
        if cached is not None and is_fresh(cached["fetched_at"], ttl):
            return [
                DiscoveryCandidate(**c).model_dump()
                for c in cached["candidates"][:topn]
            ]
        if offline:
            if cached is None:
                raise RegistryError("retryable", "offline cache miss")
            return [
                DiscoveryCandidate(**c).model_dump()
                for c in cached["candidates"][:topn]
            ]
        ecos = [ecosystem] if ecosystem else ["npm", "maven", "pypi"]
        gathered: list[DiscoveryCandidate] = []
        failures = 0
        for eco in ecos:
            try:
                gathered.extend(await self._search_one(client, eco, query))
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
        return [c.model_dump() for c in ranked[:topn]]

    def _github_eligible(self, ecosystem: str | None) -> bool:
        from libref.config import setting_or_default

        if ecosystem is not None:
            return False
        token = setting_or_default(self._settings, "discovery_github_token")
        return bool(token and str(token).strip())

    async def _search_one(
        self, client, ecosystem: str, query: str
    ) -> list[DiscoveryCandidate]:
        if ecosystem == "pypi":
            return await self._search_pypi(client, query)
        if ecosystem == "npm":
            return await self._search_npm(client, query)
        if ecosystem == "maven":
            return await self._search_maven(client, query)
        raise ValueError(f"unsupported ecosystem: {ecosystem}")

    async def _search_pypi(self, client, query: str) -> list[DiscoveryCandidate]:
        from libref.crawler.registry import fetch_snapshot

        try:
            snap = await fetch_snapshot("pypi", query.strip(), client, self._settings)
        except RegistryError as exc:
            if exc.kind == "not_found":
                return []
            raise
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

    async def _search_npm(self, client, query: str) -> list[DiscoveryCandidate]:
        from libref.crawler.http import resilient_get
        from libref.crawler.registry import fetch_snapshot, is_repo_host_url
        from libref.crawler.registry.npm import npm_search_url, parse_npm_search

        try:
            resp = await resilient_get(
                client,
                npm_search_url(query, 20),
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
                        "npm", item["name"], client, self._settings
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

    async def _search_maven(self, client, query: str) -> list[DiscoveryCandidate]:
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
                        "maven", item["name"], client, self._settings
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
                github_search_url(query, 10),
                headers=github_search_headers(token),
            )
        except httpx.HTTPError as exc:
            raise RegistryError(
                "retryable", f"github search unreachable: {exc}"
            ) from None
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"github search HTTP {resp.status_code}")
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
            for item in parse_github_search(resp.json())
        ]

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
