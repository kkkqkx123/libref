"""Library resolution orchestration: check cache, call client, write cache, apply override rules.

The only place that reads and writes the registry cache table. Library metadata rows are maintained by the search service's library-version entry; this module only returns resolution conclusions and does not write library tables directly.
"""

from __future__ import annotations

import json
import sqlite3
import time

from libref.crawler.registry import (
    RegistryError,
    Resolution,
    best_effort_stable,
    fetch_snapshot,
    normalize_name,
    sanitize_http_url,
)
from libref.crawler.registry.overrides import ensure_overrides_file, get_override


def infer_ecosystem(explicit: str | None, name: str) -> str:
    """Ecosystem determination: explicit wins; bare names are not guessed; only scope and coordinate shape are inferable."""
    if explicit is not None:
        eco = explicit.strip().lower()
        if eco not in ("pypi", "npm", "maven"):
            raise ValueError(f"unsupported ecosystem: {explicit}")
        if eco == "pypi" and name.strip().startswith("@"):
            raise ValueError("pypi package must not start with '@'")
        if eco == "maven" and ":" not in name:
            raise ValueError("maven name must look like group:artifact")
        return eco
    stripped = name.strip()
    if stripped.startswith("@") and "/" in stripped:
        return "npm"
    if ":" in stripped:
        return "maven"
    raise ValueError("ecosystem required: pass --ecosystem pypi|npm|maven")


def read_cache(conn: sqlite3.Connection, ecosystem: str, name: str) -> dict | None:
    row = conn.execute(
        "SELECT docs_url, candidates, versions, stable, homepage, repo_url, fetched_at "
        "FROM registry_cache WHERE ecosystem=? AND name=?",
        (ecosystem, name),
    ).fetchone()
    if row is None:
        return None
    return {
        "docs_url": row[0],
        "candidates": json.loads(row[1]) if row[1] else [],
        "versions": json.loads(row[2]) if row[2] else [],
        "stable": row[3],
        "homepage": row[4],
        "repo_url": row[5],
        "fetched_at": row[6],
    }


def write_cache(
    conn: sqlite3.Connection,
    *,
    ecosystem: str,
    name: str,
    docs_url: str | None,
    candidates: list[str],
    versions: list[str],
    stable: str | None,
    homepage: str | None,
    repo_url: str | None,
    fetched_at: float,
) -> None:
    conn.execute(
        "INSERT INTO registry_cache(ecosystem, name, docs_url, candidates, versions, "
        "stable, homepage, repo_url, fetched_at) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ecosystem, name) DO UPDATE SET docs_url=excluded.docs_url, "
        "candidates=excluded.candidates, versions=excluded.versions, stable=excluded.stable, "
        "homepage=excluded.homepage, repo_url=excluded.repo_url, "
        "fetched_at=excluded.fetched_at",
        (
            ecosystem,
            name,
            docs_url,
            json.dumps(candidates, ensure_ascii=False),
            json.dumps(versions, ensure_ascii=False),
            stable,
            homepage,
            repo_url,
            fetched_at,
        ),
    )
    conn.commit()


def _is_fresh(fetched_at: float, ttl_days: float) -> bool:
    return (time.time() - fetched_at) < ttl_days * 86400


def _pick_version(
    versions: list[str], stable: str | None, requested: str | None
) -> tuple[str, bool]:
    if requested is not None:
        return requested, requested in versions
    if stable:
        return stable, True
    best = best_effort_stable(versions)
    if best is not None:
        return best, True
    raise RegistryError("no_docs", "registry lists no versions")


def _build_resolution(
    *,
    ecosystem: str,
    name: str,
    requested: str | None,
    versions: list[str],
    stable: str | None,
    homepage: str | None,
    repo_url: str | None,
    override_docs: str | None,
    override_repo: str | None,
    registry_docs: str | None,
    candidates: list[str],
    source: str,
) -> Resolution:
    docs_url = override_docs or registry_docs
    repo = override_repo or repo_url
    if docs_url is None:
        raise RegistryError(
            "no_docs",
            "registry has no documentation address",
            hint={"homepage": homepage, "repo_url": repo},
        )
    version, confirmed = _pick_version(versions, stable, requested)
    final_candidates = [docs_url]
    for c in candidates:
        if c != docs_url and c not in final_candidates:
            final_candidates.append(c)
    return Resolution(
        ecosystem=ecosystem,
        name=name,
        requested_version=requested,
        version=version,
        registry_confirmed=confirmed,
        docs_url=docs_url,
        candidates=final_candidates,
        homepage=homepage,
        repo_url=repo,
        source=source,
    )


def _override_addrs(
    override: dict[str, str] | None, *, allow_private: bool
) -> tuple[str | None, str | None]:
    if override is None:
        return None, None
    docs = sanitize_http_url(override.get("docs_url"), allow_private=allow_private)
    repo = sanitize_http_url(override.get("repo_url"), allow_private=allow_private)
    if repo is None and override.get("repo_url"):
        raw = str(override.get("repo_url", "")).strip().removeprefix("git+")
        repo = sanitize_http_url(raw, allow_private=allow_private)
    return docs, repo


def _registry_context(conn, settings, ecosystem: str | None, name: str):
    """Registry shared context: ecosystem inference, override addresses, cache and freshness, assembled in one place."""
    allow_private = bool(settings.registry_allow_private)
    eco = infer_ecosystem(ecosystem, name)
    norm = normalize_name(eco, name)
    if not norm:
        raise ValueError("empty package name")
    overrides_file = settings.registry_overrides_file
    if overrides_file is not None:
        ensure_overrides_file(overrides_file)
    override_docs, override_repo = _override_addrs(
        get_override(eco, norm), allow_private=allow_private
    )
    ttl = float(settings.registry_cache_ttl_days)
    offline = bool(settings.registry_offline)
    cached = read_cache(conn, eco, norm)
    return eco, norm, override_docs, override_repo, ttl, offline, cached


def _store_snapshot(conn, eco: str, norm: str, snap) -> None:
    """Store the registry snapshot in cache; shared by both the docs and repo paths."""
    write_cache(
        conn,
        ecosystem=eco,
        name=norm,
        docs_url=snap.doc_candidates[0] if snap.doc_candidates else None,
        candidates=snap.doc_candidates,
        versions=snap.versions,
        stable=snap.stable,
        homepage=snap.homepage,
        repo_url=snap.repo_url,
        fetched_at=time.time(),
    )


def _build_repo_resolution(
    *,
    ecosystem: str,
    name: str,
    requested: str | None,
    versions: list[str],
    stable: str | None,
    homepage: str | None,
    repo_url: str | None,
    override_docs: str | None,
    override_repo: str | None,
    registry_docs: str | None,
    candidates: list[str],
    source: str,
) -> Resolution:
    """Repo resolution assembly: override repo wins; missing repo raises no_docs directly, symmetric with the docs resolution."""
    repo = override_repo or repo_url
    if repo is None:
        raise RegistryError(
            "no_docs",
            "registry has no repository address",
            hint={"homepage": homepage},
        )
    version, confirmed = _pick_version(versions, stable, requested)
    return Resolution(
        ecosystem=ecosystem,
        name=name,
        requested_version=requested,
        version=version,
        registry_confirmed=confirmed,
        docs_url=override_docs or registry_docs,
        candidates=list(candidates),
        homepage=homepage,
        repo_url=repo,
        source=source,
    )


async def resolve_package(
    conn: sqlite3.Connection,
    client,
    settings,
    ecosystem: str | None,
    name: str,
    requested_version: str | None = None,
    explicit_url: str | None = None,
) -> Resolution:
    """Single entry point for name resolution: explicit addresses pass through, the rest go through overrides + cache + registry."""
    allow_private = bool(settings.registry_allow_private)
    if explicit_url is not None:
        if requested_version is None:
            raise ValueError("explicit address requires a version")
        clean = sanitize_http_url(explicit_url, allow_private=allow_private)
        if clean is None:
            raise ValueError(f"invalid documentation address: {explicit_url}")
        eco = ecosystem.strip().lower() if ecosystem else "custom"
        label = name.strip()
        if eco in ("pypi", "npm", "maven"):
            label = normalize_name(eco, name)
        return Resolution(
            ecosystem=eco,
            name=label,
            requested_version=requested_version,
            version=requested_version,
            registry_confirmed=True,
            docs_url=clean,
            candidates=[clean],
            source="explicit",
        )

    eco, norm, override_docs, override_repo, ttl, offline, cached = _registry_context(
        conn, settings, ecosystem, name
    )
    override_hit = bool(override_docs or override_repo)
    if offline:
        if cached is None:
            raise RegistryError("retryable", "offline cache miss")
        return _build_resolution(
            ecosystem=eco,
            name=norm,
            requested=requested_version,
            versions=cached["versions"],
            stable=cached["stable"],
            homepage=cached["homepage"],
            repo_url=cached["repo_url"],
            override_docs=override_docs,
            override_repo=override_repo,
            registry_docs=cached["docs_url"],
            candidates=cached["candidates"],
            source="override" if override_hit else "registry",
        )
    if cached is not None and _is_fresh(cached["fetched_at"], ttl):
        return _build_resolution(
            ecosystem=eco,
            name=norm,
            requested=requested_version,
            versions=cached["versions"],
            stable=cached["stable"],
            homepage=cached["homepage"],
            repo_url=cached["repo_url"],
            override_docs=override_docs,
            override_repo=override_repo,
            registry_docs=cached["docs_url"],
            candidates=cached["candidates"],
            source="override" if override_hit else "registry",
        )
    try:
        snap = await fetch_snapshot(eco, norm, client, settings)
    except RegistryError as exc:
        if exc.kind == "retryable" and cached is not None:
            return _build_resolution(
                ecosystem=eco,
                name=norm,
                requested=requested_version,
                versions=cached["versions"],
                stable=cached["stable"],
                homepage=cached["homepage"],
                repo_url=cached["repo_url"],
                override_docs=override_docs,
                override_repo=override_repo,
                registry_docs=cached["docs_url"],
                candidates=cached["candidates"],
                source="override" if override_hit else "registry-stale",
            )
        raise
    _store_snapshot(conn, eco, norm, snap)
    return _build_resolution(
        ecosystem=eco,
        name=norm,
        requested=requested_version,
        versions=snap.versions,
        stable=snap.stable,
        homepage=snap.homepage,
        repo_url=snap.repo_url,
        override_docs=override_docs,
        override_repo=override_repo,
        registry_docs=snap.doc_candidates[0] if snap.doc_candidates else None,
        candidates=snap.doc_candidates,
        source="override" if override_hit else "registry",
    )


async def resolve_repo(
    conn: sqlite3.Connection,
    client,
    settings,
    ecosystem: str | None,
    name: str,
    requested_version: str | None = None,
    explicit_source: str | None = None,
) -> Resolution:
    from pathlib import Path

    allow_private = bool(settings.registry_allow_private)
    if explicit_source is not None:
        text = str(explicit_source).strip()
        if not text:
            raise ValueError("empty repo source")
        if not Path(text).is_dir():
            cleaned = sanitize_http_url(text, allow_private=allow_private)
            if cleaned is None:
                cleaned = sanitize_http_url(
                    text.removeprefix("git+"), allow_private=allow_private
                )
            if cleaned is None:
                raise ValueError(f"invalid repo address: {explicit_source}")
            text = cleaned
        if requested_version is None:
            raise ValueError("explicit repo source requires a version")
        eco = ecosystem.strip().lower() if ecosystem else "custom"
        label = name.strip()
        if eco in ("pypi", "npm", "maven"):
            label = normalize_name(eco, name)
        return Resolution(
            ecosystem=eco,
            name=label,
            requested_version=requested_version,
            version=requested_version,
            registry_confirmed=True,
            docs_url=None,
            candidates=[],
            source="explicit",
            repo_url=text,
        )
    eco, norm, override_docs, override_repo, ttl, offline, cached = _registry_context(
        conn, settings, ecosystem, name
    )
    override_hit = bool(override_repo)
    if offline:
        if cached is None:
            raise RegistryError("retryable", "offline cache miss")
        return _build_repo_resolution(
            ecosystem=eco,
            name=norm,
            requested=requested_version,
            versions=cached["versions"],
            stable=cached["stable"],
            homepage=cached["homepage"],
            repo_url=cached["repo_url"],
            override_docs=override_docs,
            override_repo=override_repo,
            registry_docs=cached["docs_url"],
            candidates=cached["candidates"],
            source="override" if override_hit else "registry",
        )
    if cached is not None and _is_fresh(cached["fetched_at"], ttl):
        repo_url = override_repo or cached["repo_url"]
        if repo_url is not None:
            return _build_repo_resolution(
                ecosystem=eco,
                name=norm,
                requested=requested_version,
                versions=cached["versions"],
                stable=cached["stable"],
                homepage=cached["homepage"],
                repo_url=cached["repo_url"],
                override_docs=override_docs,
                override_repo=override_repo,
                registry_docs=cached["docs_url"],
                candidates=cached["candidates"],
                source="override" if override_hit else "registry",
            )
    try:
        snap = await fetch_snapshot(eco, norm, client, settings)
    except RegistryError as exc:
        if exc.kind == "retryable" and cached is not None:
            repo_url = override_repo or cached["repo_url"]
            if repo_url is None:
                raise RegistryError(
                    "no_docs",
                    "registry has no repository address",
                    hint={"homepage": cached["homepage"]},
                ) from None
            return _build_repo_resolution(
                ecosystem=eco,
                name=norm,
                requested=requested_version,
                versions=cached["versions"],
                stable=cached["stable"],
                homepage=cached["homepage"],
                repo_url=cached["repo_url"],
                override_docs=override_docs,
                override_repo=override_repo,
                registry_docs=cached["docs_url"],
                candidates=cached["candidates"],
                source="override" if override_hit else "registry-stale",
            )
        raise
    _store_snapshot(conn, eco, norm, snap)
    return _build_repo_resolution(
        ecosystem=eco,
        name=norm,
        requested=requested_version,
        versions=snap.versions,
        stable=snap.stable,
        homepage=snap.homepage,
        repo_url=snap.repo_url,
        override_docs=override_docs,
        override_repo=override_repo,
        registry_docs=snap.doc_candidates[0] if snap.doc_candidates else None,
        candidates=list(snap.doc_candidates),
        source="override" if override_hit else "registry",
    )


async def resolve(
    conn: sqlite3.Connection,
    client,
    settings,
    ecosystem: str | None,
    name: str,
    requested_version: str | None = None,
    target: str = "docs",
) -> Resolution:
    """Unified resolve: documentation address by default, repository address when requested."""
    if target == "docs":
        return await resolve_package(
            conn, client, settings, ecosystem, name, requested_version
        )
    if target == "repo":
        return await resolve_repo(
            conn, client, settings, ecosystem, name, requested_version
        )
    raise ValueError(f"unsupported resolve target: {target}")
