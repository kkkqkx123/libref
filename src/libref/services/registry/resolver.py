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
    """Ecosystem determination: explicit wins; bare names are not guessed; only scope, coordinate, and module-path shapes are inferable."""
    from libref.crawler.registry import SUPPORTED_ECOSYSTEMS, canonical_ecosystem

    if explicit is not None:
        eco = canonical_ecosystem(explicit)
        if eco not in SUPPORTED_ECOSYSTEMS:
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
    if "/" in stripped:
        return "go"
    raise ValueError(
        "ecosystem required: pass --ecosystem pypi|npm|maven|go|crates|nuget"
    )


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
    from libref.db.conn import write_lock

    with write_lock(conn):
        _write_cache_locked(
            conn,
            ecosystem=ecosystem,
            name=name,
            docs_url=docs_url,
            candidates=candidates,
            versions=versions,
            stable=stable,
            homepage=homepage,
            repo_url=repo_url,
            fetched_at=fetched_at,
        )


def _write_cache_locked(
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


def is_fresh(fetched_at: float, ttl_days: float) -> bool:
    return (time.time() - fetched_at) < ttl_days * 86400


def _pick_version(
    versions: list[str],
    stable: str | None,
    requested: str | None,
    include_prerelease: bool = False,
) -> tuple[str, bool]:
    if requested is not None:
        return requested, requested in versions
    if include_prerelease:
        best = best_effort_stable(versions, include_prerelease=True)
        if best is not None:
            return best, True
    if stable:
        return stable, True
    best = best_effort_stable(versions)
    if best is not None:
        return best, True
    raise RegistryError("no_docs", "registry lists no versions")


def _context_hint(
    ecosystem: str | None, name: str, requested: str | None = None
) -> dict:
    hint: dict = {"ecosystem": ecosystem, "name": name}
    if requested is not None:
        hint["requested_version"] = requested
    return hint


def next_actions_for(
    ecosystem: str | None, name: str, target: str, kind: str
) -> list[dict]:
    """Structured follow-ups for humans and agents: fuzzy discovery first, then manual search with an explicit address."""
    from libref.crawler.registry import SUPPORTED_ECOSYSTEMS

    query = (name or "").strip()
    scoped = ecosystem in SUPPORTED_ECOSYSTEMS
    discover_cli = f'libref discover "{query}"'
    arguments: dict = {"query": query}
    if scoped:
        discover_cli += f" --ecosystem {ecosystem}"
        arguments["ecosystem"] = ecosystem
    if kind == "not_found":
        purpose = "fuzzy discovery across registries; check spelling and find the canonical package"
    else:
        purpose = "fuzzy discovery across registries; confirm the canonical package and its metadata"
    actions = [
        {
            "kind": "tool",
            "mcp_tool": "library_search",
            "cli": discover_cli,
            "arguments": arguments,
            "purpose": purpose,
        }
    ]
    if kind == "retryable":
        actions.append(
            {
                "kind": "manual",
                "detail": "the registry is unreachable; retry later or rerun with a warm cache",
            }
        )
    if target == "repo":
        actions.append(
            {
                "kind": "tool",
                "mcp_tool": "github_search",
                "cli": f'libref search-github "{query}"',
                "arguments": {"query": query},
                "purpose": "hosted search for clonable repositories; results are unverified, confirm then use an explicit source",
            }
        )
        actions.append(
            {
                "kind": "manual",
                "detail": f"search the web for the official source repository of {query} "
                "(or run libref search-github / MCP github_search), "
                "then rerun with an explicit source "
                "(libref index-repo <library> <version> <url-or-local-path>), "
                "or add a repo_url entry to the registry overrides file",
            }
        )
    else:
        actions.append(
            {
                "kind": "tool",
                "mcp_tool": "web_search",
                "cli": f'libref search-web "{query}"',
                "arguments": {"query": query},
                "purpose": "keyless web search for docs or repos; results are unverified, confirm then use an explicit address",
            }
        )
        actions.append(
            {
                "kind": "manual",
                "detail": f"search the web for the official documentation of {query} "
                "(or run libref search-web / MCP web_search), "
                "then rerun with an explicit address "
                "(libref ingest <library> <version> <url>), "
                "or add a docs_url entry to the registry overrides file",
            }
        )
    return actions


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
    include_prerelease: bool = False,
) -> Resolution:
    docs_url = override_docs or registry_docs
    repo = override_repo or repo_url
    if docs_url is None:
        hint = _context_hint(ecosystem, name, requested)
        if homepage is not None:
            hint["homepage"] = homepage
        if repo is not None:
            hint["repo_url"] = repo
        raise RegistryError(
            "no_docs",
            "registry has no documentation address",
            hint=hint,
        )
    try:
        version, confirmed = _pick_version(
            versions, stable, requested, include_prerelease
        )
    except RegistryError as exc:
        if not exc.hint:
            exc.hint = _context_hint(ecosystem, name, requested)
        raise
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
    include_prerelease: bool = False,
) -> Resolution:
    """Repo resolution assembly: override repo wins; missing repo raises no_docs directly, symmetric with the docs resolution."""
    repo = override_repo or repo_url
    if repo is None:
        hint = _context_hint(ecosystem, name, requested)
        if homepage is not None:
            hint["homepage"] = homepage
        raise RegistryError(
            "no_docs",
            "registry has no repository address",
            hint=hint,
        )
    try:
        version, confirmed = _pick_version(
            versions, stable, requested, include_prerelease
        )
    except RegistryError as exc:
        if not exc.hint:
            exc.hint = _context_hint(ecosystem, name, requested)
        raise
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


def _builder_for(target: str):
    if target == "docs":
        return _build_resolution
    if target == "repo":
        return _build_repo_resolution
    raise ValueError(f"unsupported resolve target: {target}")


def _override_hit_for(
    target: str, override_docs: str | None, override_repo: str | None
) -> bool:
    if target == "docs":
        return bool(override_docs or override_repo)
    return bool(override_repo)


def _explicit_docs_resolution(
    ecosystem: str | None,
    name: str,
    requested_version: str | None,
    explicit_url: str,
    *,
    allow_private: bool,
) -> Resolution:
    if requested_version is None:
        raise ValueError("explicit address requires a version")
    clean = sanitize_http_url(explicit_url, allow_private=allow_private)
    if clean is None:
        raise ValueError(f"invalid documentation address: {explicit_url}")
    from libref.crawler.registry import canonical_ecosystem

    eco = canonical_ecosystem(ecosystem) if ecosystem else "custom"
    if eco is None:
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


def _explicit_repo_resolution(
    ecosystem: str | None,
    name: str,
    requested_version: str | None,
    explicit_source: str,
    *,
    allow_private: bool,
) -> Resolution:
    from pathlib import Path

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
    from libref.crawler.registry import canonical_ecosystem as _canonical

    eco = _canonical(ecosystem) if ecosystem else "custom"
    if eco is None:
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


async def _resolve_via_registry(
    conn,
    client,
    settings,
    ecosystem: str | None,
    name: str,
    requested_version: str | None,
    target: str,
    include_prerelease: bool = False,
) -> Resolution:
    eco, norm, override_docs, override_repo, ttl, offline, cached = _registry_context(
        conn, settings, ecosystem, name
    )
    override_hit = _override_hit_for(target, override_docs, override_repo)
    builder = _builder_for(target)
    if offline:
        if cached is None:
            raise RegistryError(
                "retryable",
                "offline cache miss",
                hint=_context_hint(eco, norm, requested_version),
            )
        return builder(
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
            include_prerelease=include_prerelease,
        )
    # A fresh canonical cache serves the default path only: a flag query refetches so
    # the resolved version and its documentation address stay coherent.
    if (
        cached is not None
        and is_fresh(cached["fetched_at"], ttl)
        and not include_prerelease
    ):
        if target == "repo" and (override_repo or cached["repo_url"]) is None:
            pass
        else:
            return builder(
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
                include_prerelease=include_prerelease,
            )
    try:
        snap = await fetch_snapshot(eco, norm, client, settings, include_prerelease)
    except RegistryError as exc:
        if not exc.hint:
            exc.hint = _context_hint(eco, norm, requested_version)
        if exc.kind == "retryable" and cached is not None:
            if target == "repo" and (override_repo or cached["repo_url"]) is None:
                hint = _context_hint(eco, norm, requested_version)
                if cached["homepage"] is not None:
                    hint["homepage"] = cached["homepage"]
                raise RegistryError(
                    "no_docs",
                    "registry has no repository address",
                    hint=hint,
                ) from None
            return builder(
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
                include_prerelease=include_prerelease,
            )
        raise
    if not include_prerelease:
        _store_snapshot(conn, eco, norm, snap)
    registry_docs = snap.doc_candidates[0] if snap.doc_candidates else None
    return builder(
        ecosystem=eco,
        name=norm,
        requested=requested_version,
        versions=snap.versions,
        stable=snap.stable,
        homepage=snap.homepage,
        repo_url=snap.repo_url,
        override_docs=override_docs,
        override_repo=override_repo,
        registry_docs=registry_docs,
        candidates=list(snap.doc_candidates),
        source="override" if override_hit else "registry",
        include_prerelease=include_prerelease,
    )


async def resolve_package(
    conn: sqlite3.Connection,
    client,
    settings,
    ecosystem: str | None,
    name: str,
    requested_version: str | None = None,
    explicit_url: str | None = None,
    include_prerelease: bool = False,
) -> Resolution:
    """Single entry point for name resolution: explicit addresses pass through, the rest go through overrides + cache + registry."""
    if explicit_url is not None:
        return _explicit_docs_resolution(
            ecosystem,
            name,
            requested_version,
            explicit_url,
            allow_private=bool(settings.registry_allow_private),
        )
    return await _resolve_via_registry(
        conn,
        client,
        settings,
        ecosystem,
        name,
        requested_version,
        "docs",
        include_prerelease,
    )


async def resolve_repo(
    conn: sqlite3.Connection,
    client,
    settings,
    ecosystem: str | None,
    name: str,
    requested_version: str | None = None,
    explicit_source: str | None = None,
    include_prerelease: bool = False,
) -> Resolution:
    if explicit_source is not None:
        return _explicit_repo_resolution(
            ecosystem,
            name,
            requested_version,
            explicit_source,
            allow_private=bool(settings.registry_allow_private),
        )
    return await _resolve_via_registry(
        conn,
        client,
        settings,
        ecosystem,
        name,
        requested_version,
        "repo",
        include_prerelease,
    )


async def resolve(
    conn: sqlite3.Connection,
    client,
    settings,
    ecosystem: str | None,
    name: str,
    requested_version: str | None = None,
    target: str = "docs",
    include_prerelease: bool = False,
) -> Resolution:
    """Unified resolve: documentation address by default, repository address when requested."""
    if target == "docs":
        return await resolve_package(
            conn,
            client,
            settings,
            ecosystem,
            name,
            requested_version,
            include_prerelease=include_prerelease,
        )
    if target == "repo":
        return await resolve_repo(
            conn,
            client,
            settings,
            ecosystem,
            name,
            requested_version,
            include_prerelease=include_prerelease,
        )
    raise ValueError(f"unsupported resolve target: {target}")
