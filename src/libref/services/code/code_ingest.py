"""Source code ingestion: clone repos, extract calls, persist entities and chunks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from libref.services.code.repo_chunks import _store_source_chunks, _sweep_source_pages
from libref.services.code.repo_collect import (
    _collect_decl_index,
    _load_raw_batch,
)
from libref.services.code.repo_ids import call_uid, entity_uid
from libref.services.code.repo_persist import (
    _check_manifest_identity,
    _persist_entities,
    _persist_import_edges,
    _persist_repo_deps,
    _sweep_calls,
    _upsert_calls,
)
from libref.services.code.repo_source import (
    list_repo_dependencies,
    lookup_index_source_cached,
)
from libref.services.infra.jobs import INDEX_REPO_KIND

logger = logging.getLogger(__name__)

INDEX_REPO_JOB_KIND = INDEX_REPO_KIND


def _same_source(left: str | None, right: str | None) -> bool:
    """Source identity ignoring a git prefix and trailing slashes."""

    def _norm(value: str | None) -> str:
        return (value or "").strip().removeprefix("git+").rstrip("/")

    both = (_norm(left), _norm(right))
    return bool(both[0]) and both[0] == both[1]


__all__ = [
    "INDEX_REPO_JOB_KIND",
    "_collect_decl_index",
    "_load_raw_batch",
    "_persist_entities",
    "_persist_import_edges",
    "_store_source_chunks",
    "_sweep_calls",
    "_sweep_source_pages",
    "_upsert_calls",
    "build_repo_runner",
    "call_uid",
    "entity_uid",
    "index_repo",
    "index_repo_async",
    "index_repo_by_name",
    "index_repo_by_name_async",
    "list_repo_dependencies",
    "lookup_index_source_cached",
]


@asynccontextmanager
async def _client(settings) -> AsyncIterator[httpx.AsyncClient]:
    from libref.crawler.http import build_async_client

    async with build_async_client(settings) as client:
        yield client


async def index_repo_async(
    conn,
    library: str,
    version: str,
    source: str | None = None,
    *,
    update: bool = False,
    repos_dir: Path,
    ecosystem: str = "custom",
    resolved_from: str | None = None,
    registry_confirmed: bool = True,
    repos_quota_mb: int | None = None,
    settings=None,
    vec_store=None,
    embed=None,
    fresh: bool = False,
    subdir: str | None = None,
    only_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    max_files: int | None = None,
    max_file_bytes: int = 1_000_000,
    heartbeat=None,
    tenant_id: str = "default",
    source_role: str = "implementation",
    origin_kind: str = "repo_file",
    target_filter: dict | None = None,
    warm_ast: bool = False,
) -> dict[str, int]:
    """Lock one version across doc and code writers, then run the ingest."""
    from libref.services.infra.version_lock import version_lock

    _stale_after = float(getattr(settings, "heartbeat_timeout", 600.0) or 600.0)
    from libref.services.infra.tenant import normalize_tenant as _ntc

    tenant_id = _ntc(tenant_id)
    async with version_lock(
        conn, ecosystem, library, version, stale_after=_stale_after, tenant_id=tenant_id
    ):
        return await _index_repo_inner(
            conn,
            library,
            version,
            source,
            update=update,
            repos_dir=repos_dir,
            ecosystem=ecosystem,
            resolved_from=resolved_from,
            registry_confirmed=registry_confirmed,
            repos_quota_mb=repos_quota_mb,
            settings=settings,
            vec_store=vec_store,
            embed=embed,
            fresh=fresh,
            subdir=subdir,
            only_files=only_files,
            exclude_patterns=exclude_patterns,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            heartbeat=heartbeat,
            tenant_id=tenant_id,
            source_role=source_role,
            origin_kind=origin_kind,
            target_filter=target_filter,
            warm_ast=warm_ast,
        )


async def _index_repo_inner(
    conn,
    library: str,
    version: str,
    source: str | None = None,
    *,
    update: bool = False,
    repos_dir: Path,
    ecosystem: str = "custom",
    resolved_from: str | None = None,
    registry_confirmed: bool = True,
    repos_quota_mb: int | None = None,
    settings=None,
    vec_store=None,
    embed=None,
    fresh: bool = False,
    subdir: str | None = None,
    only_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    max_files: int | None = None,
    max_file_bytes: int = 1_000_000,
    heartbeat=None,
    tenant_id: str = "default",
    source_role: str = "implementation",
    origin_kind: str = "repo_file",
    target_filter: dict | None = None,
    warm_ast: bool = False,
) -> dict[str, int]:
    from libref.astminer import cache as ast_cache
    from libref.astminer import config as ast_config
    from libref.astminer import resolution as ast_resolution
    from libref.astminer.repos import RepoManager, describe_source_files
    from libref.services.infra.progress import (
        CODE_KIND,
        resume_offset,
        save_progress,
    )
    from libref.services.infra.tenant import DEFAULT_TENANT as _DT
    from libref.services.registry.libraries import LibraryService

    _safe_t = (
        "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in str(tenant_id))[
            :64
        ]
        or _DT
    )
    settings_dir = (
        Path(repos_dir)
        if str(tenant_id or "default") == _DT
        else (Path(repos_dir) / f"tenant-{_safe_t}")
    )
    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if not source:
        hit = lookup_index_source_cached(conn, ecosystem, library, version, settings)
        if hit is None:
            raise ValueError(
                "repo source unknown: pass an explicit address or resolve by name "
                "with --ecosystem (needs registry cache or network)"
            )
        source, cached_origin = hit
        if resolved_from is None:
            resolved_from = cached_origin
    if resolved_from is None:
        resolved_from = "explicit"
    from libref.services.code.usage_sources import (
        register_source,
        source_target_status,
        update_source_commit,
    )

    catalog = LibraryService(conn)
    library_id, version_id = catalog.ensure_library_version(
        library,
        version,
        ecosystem,
        repo_url=source,
        resolved_from=resolved_from,
        registry_confirmed=registry_confirmed,
        tenant_id=tenant_id,
    )
    import hashlib as _hashlib
    import json as _json

    target_text = _json.dumps(
        target_filter or {}, sort_keys=True, separators=(",", ":")
    )
    source_fingerprint = _hashlib.sha1(
        (
            f"{source}\0{subdir or ''}\0{','.join(only_files or [])}"
            f"\0{','.join(exclude_patterns or [])}\0{max_file_bytes}"
            f"\0{target_text}\0{origin_kind}"
        ).encode()
    ).hexdigest()[:12]
    source_id = register_source(
        conn,
        version_id=version_id,
        source_key=f"{source_role}:{source_fingerprint}",
        source_role=source_role,
        origin_kind=origin_kind,
        source_url=str(source),
        selection={
            "subdir": subdir,
            "only_files": only_files or [],
            "exclude_patterns": exclude_patterns or [],
            "max_file_bytes": int(max_file_bytes),
        },
        target_filter=target_filter or {},
        tenant_id=tenant_id,
    )
    counts_source = {"source_id": source_id}
    conn.commit()
    if repos_quota_mb is not None:
        from libref.services.ops.retention import check_quota

        check_quota(settings_dir, int(repos_quota_mb), "repos")
    if settings is not None and bool(getattr(settings, "quota_enabled", False)):
        from libref.crawler.scope import BudgetExceeded as _BudgetExceeded
        from libref.services.infra import metrics as _metrics
        from libref.services.infra import quota as _quota

        _decision = _quota.check(
            conn,
            tenant_id=tenant_id,
            principal_id=None,
            resource="repos_bytes",
            settings=settings,
        )
        if not _decision.allowed:
            _metrics.incr("quota_denied_total{resource=repos_bytes}")
            _quota.record_event(
                conn,
                tenant_id=tenant_id,
                principal_id=None,
                resource="repos_bytes",
                result="denied",
                used=_decision.used,
                limit_value=_decision.limit_value,
            )
            raise _BudgetExceeded(
                f"repos_bytes quota exceeded for tenant {tenant_id}:"
                f" {_decision.used} >= {_decision.limit_value}"
            )

    def _beat() -> None:
        if heartbeat is None:
            return
        try:
            maybe = heartbeat()
            if asyncio.iscoroutine(maybe):
                asyncio.get_running_loop().create_task(maybe)
        except Exception:  # noqa: BLE001, S110 — heartbeat only aims to renew
            pass

    _beat()
    manager = RepoManager(settings_dir)
    base_repo_name = f"{ecosystem}/{library}/{version}"
    repo_name = (
        base_repo_name
        if source_role == "implementation"
        else f"{base_repo_name}/usage/{source_role}/{source_id}"
    )
    existing = conn.execute(
        "SELECT url, local_path FROM repos WHERE name=?", (repo_name,)
    ).fetchone()
    if existing is not None:
        old_url, old_path = existing[0], existing[1]
        if not _same_source(old_url, source) and not _same_source(
            str(source), str(old_path or "")
        ):
            raise ValueError(
                f"version already indexed from different source: {old_url}"
            )
    root, commit = manager.ensure(conn, repo_name, source, update=update)
    update_source_commit(conn, source_id, commit)
    if settings is not None and bool(getattr(settings, "quota_enabled", False)):
        from libref.services.infra.quota import add_usage as _add_repo_usage
        from libref.services.ops.retention import dir_size_bytes as _dir_size

        try:
            _add_repo_usage(
                conn,
                tenant_id=tenant_id,
                principal_id=None,
                resource="repos_bytes",
                amount=_dir_size(Path(root)),
            )
        except Exception:  # noqa: BLE001, S110 — accounting must never abort indexing
            pass
    _beat()
    conn.commit()
    from libref.crawler.scope import BudgetExceeded

    if exclude_patterns is not None:
        for item in exclude_patterns:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("exclude_patterns must be non-empty strings")
    census = describe_source_files(
        root,
        ast_config.all_suffixes(),
        subdir=subdir,
        exclude_patterns=exclude_patterns,
        max_file_bytes=max_file_bytes,
    )
    files = list(census["files"])
    if only_files:
        wanted_files = set(only_files)
        files = [r for r in files if r in wanted_files]
    if max_files is not None and len(files) > max_files:
        raise BudgetExceeded(
            f"source budget exceeded: {len(files)} files over budget {max_files}"
        )
    if warm_ast:
        from libref.astminer import engine as ast_engine

        by_name: dict[str, Any] = {}
        for rel in files:
            cfg = ast_config.config_for_suffix(Path(rel).suffix)
            if cfg is not None:
                by_name[cfg.name] = cfg
        ast_engine.warm_configs(by_name.values())
    if settings is not None and bool(getattr(settings, "usage_mining_enabled", True)):
        from libref.astminer.code_blocks import rust_doc_blocks
        from libref.services.code.usage_ingest import ingest_snippets

        rust_snippets: list = []
        for rel in files:
            if not rel.endswith(".rs"):
                continue
            try:
                raw = (root / rel).read_bytes()
            except OSError:
                continue
            rust_snippets.extend(rust_doc_blocks(raw, path=rel))
        if rust_snippets:
            ingest_snippets(
                conn,
                library,
                version,
                rust_snippets,
                source_key=f"rustdoc:{version_id}",
                source_role="doc_snippet",
                origin_kind="rustdoc_doctest",
                ecosystem=ecosystem,
                tenant_id=tenant_id,
                source_url=str(source),
                commit_sha=commit,
                settings=settings,
            )
    batch_size = max(1, int(getattr(settings, "index_batch_files", 200) or 200))
    done, fingerprint = resume_offset(conn, version_id, CODE_KIND, files, fresh=fresh)
    counts: dict[str, Any] = {
        "resumed_skipped": done,
        "batches": 0,
        "vector_failed": 0,
    }
    counts["files_truncated"] = 0
    counts["skipped_oversize"] = int(census.get("oversized", 0))
    counts["census_files"] = len(files) + int(census.get("oversized", 0))
    counts["skipped_paths"] = list(census.get("skipped_paths") or [])
    if counts["skipped_oversize"]:
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "skipped %d oversize source files under %s",
            counts["skipped_oversize"],
            root,
        )
    code_search = source_role == "implementation"
    _vectors_live = (
        code_search
        and settings is not None
        and vec_store is not None
        and embed is not None
    )
    _doc_store = None
    if _vectors_live:
        from libref.services.ops.maintenance import (
            sync_vectors_for_docs as _sync_docs,
        )
        from libref.store.document_store import DocumentStore as _DocumentStore

        _doc_store = _DocumentStore(conn)

    all_decls, file_aliases, collect_counts, live = _collect_decl_index(
        conn,
        repo_name,
        root,
        files,
    )
    counts.update(collect_counts)
    counts.update(counts_source)
    counts["evicted"] = ast_cache.sweep_repo(conn, repo_name, live)
    conn.commit()
    persist_definitions = code_search
    filt = target_filter or {}
    explicit_names = {str(v) for v in filt.get("names", []) if str(v).strip()}
    target_prefixes = tuple(str(v) for v in filt.get("prefixes", []) if str(v).strip())
    if persist_definitions:
        n_entities, swept_entities = _persist_entities(
            conn, library_id, version_id, all_decls
        )
        counts["import_edges"] = _persist_import_edges(
            conn, library_id, version_id, file_aliases
        )
    else:
        n_entities, swept_entities = 0, 0
        counts["import_edges"] = 0
    counts["entities"] = n_entities
    counts["swept_entities"] = swept_entities
    conn.commit()
    resolution_decls = all_decls
    if persist_definitions:
        target_names = {
            str(r[0])
            for r in conn.execute(
                "SELECT qualified_name FROM api_entities WHERE version_id=?",
                (version_id,),
            ).fetchall()
        }
    else:
        from libref.services.code.usage_ingest import target_declarations

        target_decls = target_declarations(
            conn,
            version_id,
            names=explicit_names or None,
            prefixes=None if explicit_names else target_prefixes,
        )
        resolution_decls = all_decls + target_decls
        target_names = {decl.qualified_name for decl in target_decls}
    remaining = files[done:]
    source_status = source_target_status(
        source_role,
        filt.get("declared_library_version"),
        version,
        version_required=bool(filt.get("version_required")),
    )
    effective_names = explicit_names or target_names
    wanted_calls: set[str] = set()
    if done > 0:
        # Resume skips the done files, so their call identities must seed the
        # sweep set; otherwise the final sweep mistakes them for deleted calls.
        # Content drift inside the skipped range needs a fresh run.
        wanted_calls.update(
            str(r[0])
            for r in conn.execute(
                "SELECT call_uid FROM call_instances WHERE version_id=?",
                (version_id,),
            ).fetchall()
        )
    n_calls = 0
    removed_ids: list[int] = []
    inserted_ids: list[int] = []
    n_chunks = 0
    chunk_dropped = 0
    for start in range(0, len(remaining), batch_size):
        batch_files = remaining[start : start + batch_size]
        batch_aliases = {rel: file_aliases.get(rel, []) for rel in batch_files}
        batch_raw = _load_raw_batch(conn, repo_name, root, batch_files)
        resolved = ast_resolution.resolve_calls(
            resolution_decls,
            batch_aliases,
            batch_raw,
            repo_name,
            commit,
            source_id=source_id,
            sample_prefix=repo_name,
            source_role=source_role,
            origin_kind=origin_kind,
            target_names=effective_names,
            target_prefixes=target_prefixes,
            source_target_status=source_status,
            strict_target_status=bool(filt.get("require_resolved_targets")),
            stdlib_filter_enabled=bool(
                getattr(settings, "stdlib_filter_enabled", True)
            ),
            stdlib_table_version=str(getattr(settings, "stdlib_table_version", "1")),
        )
        batch_count, batch_wanted = _upsert_calls(
            conn, library_id, version_id, resolved, commit
        )
        n_calls += batch_count
        wanted_calls.update(batch_wanted)
        conn.commit()
        if code_search:
            batch_live = {rel: live[rel] for rel in batch_files if rel in live}
            batch_chunks, batch_dropped, batch_removed, batch_inserted = (
                _store_source_chunks(
                    conn,
                    repo_name,
                    root,
                    sorted(batch_live),
                    all_decls,
                    library_id,
                    version_id,
                )
            )
            n_chunks += batch_chunks
            chunk_dropped += batch_dropped
            removed_ids.extend(batch_removed)
            inserted_ids.extend(batch_inserted)
            conn.commit()
        counts["batches"] += 1
        if _vectors_live:
            await _sync_docs(
                conn,
                settings,
                vec_store,
                embed,
                _doc_store,
                counts,
                batch_removed,
                batch_inserted,
                version_id=version_id,
                verify=False,
            )
        # Sync-then-mark like the docs path: a crash never advances the
        # cursor past unsynced vectors, so resume replays the batch.
        save_progress(
            conn,
            version_id,
            CODE_KIND,
            total=len(files),
            done=done + start + len(batch_files),
            fingerprint=fingerprint,
            batch_size=batch_size,
        )
        _beat()
    swept_calls = _sweep_calls(conn, version_id, wanted_calls, source_id=source_id)
    counts["calls"] = n_calls
    counts["swept_calls"] = swept_calls
    conn.commit()
    _check_manifest_identity(ecosystem, library, root)
    n_deps = _persist_repo_deps(conn, library_id, version_id, root)
    counts["manifest_deps"] = n_deps
    swept_pages = _sweep_source_pages(conn, repo_name, set(live)) if code_search else []
    removed_ids.extend(swept_pages)
    counts["swept_pages"] = len(swept_pages)
    conn.commit()
    counts["source_chunks"] = n_chunks
    counts["chunk_empty_dropped"] = chunk_dropped
    counts["chunk_truncated"] = 0
    if _vectors_live:
        await _sync_docs(
            conn,
            settings,
            vec_store,
            embed,
            _doc_store,
            counts,
            swept_pages,
            [],
            version_id=version_id,
        )
    from libref.services.infra.progress import CODE_KIND as _CODE_KIND
    from libref.services.infra.progress import clear_progress as _clear_progress

    _clear_progress(conn, version_id, _CODE_KIND)
    return counts


def index_repo(
    conn,
    library: str,
    version: str,
    source: str | None = None,
    *,
    update: bool = False,
    repos_dir: Path,
    ecosystem: str = "custom",
    resolved_from: str | None = None,
    registry_confirmed: bool = True,
    repos_quota_mb: int | None = None,
    settings=None,
    vec_store=None,
    embed=None,
    fresh: bool = False,
    subdir: str | None = None,
    only_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    max_files: int | None = None,
    max_file_bytes: int = 1_000_000,
    heartbeat=None,
    tenant_id: str = "default",
    source_role: str = "implementation",
    origin_kind: str = "repo_file",
    target_filter: dict | None = None,
    warm_ast: bool = False,
) -> dict[str, int]:
    return asyncio.run(
        index_repo_async(
            conn,
            library,
            version,
            source,
            update=update,
            repos_dir=repos_dir,
            ecosystem=ecosystem,
            resolved_from=resolved_from,
            registry_confirmed=registry_confirmed,
            repos_quota_mb=repos_quota_mb,
            settings=settings,
            vec_store=vec_store,
            embed=embed,
            fresh=fresh,
            subdir=subdir,
            only_files=only_files,
            exclude_patterns=exclude_patterns,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            heartbeat=heartbeat,
            tenant_id=tenant_id,
            source_role=source_role,
            origin_kind=origin_kind,
            target_filter=target_filter,
            warm_ast=warm_ast,
        )
    )


def index_repo_by_name(
    conn,
    settings,
    ecosystem: str | None,
    library: str,
    version: str | None = None,
    *,
    update: bool = False,
    repos_dir=None,
    explicit_source: str | None = None,
    include_prerelease: bool = False,
    fresh: bool = False,
    subdir: str | None = None,
    only_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    max_files: int | None = None,
    max_file_bytes: int = 1_000_000,
    heartbeat=None,
    tenant_id: str = "default",
    source_role: str = "implementation",
    origin_kind: str = "repo_file",
    target_filter: dict | None = None,
    warm_ast: bool = False,
) -> dict[str, int]:
    return asyncio.run(
        index_repo_by_name_async(
            conn,
            settings,
            ecosystem,
            library,
            version,
            update=update,
            repos_dir=repos_dir,
            explicit_source=explicit_source,
            include_prerelease=include_prerelease,
            fresh=fresh,
            subdir=subdir,
            only_files=only_files,
            exclude_patterns=exclude_patterns,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            heartbeat=heartbeat,
            tenant_id=tenant_id,
            source_role=source_role,
            origin_kind=origin_kind,
            target_filter=target_filter,
            warm_ast=warm_ast,
        )
    )


async def index_repo_by_name_async(
    conn,
    settings,
    ecosystem: str | None,
    library: str,
    version: str | None = None,
    *,
    update: bool = False,
    repos_dir=None,
    explicit_source: str | None = None,
    include_prerelease: bool = False,
    fresh: bool = False,
    subdir: str | None = None,
    only_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    max_files: int | None = None,
    max_file_bytes: int = 1_000_000,
    heartbeat=None,
    tenant_id: str = "default",
    source_role: str = "implementation",
    origin_kind: str = "repo_file",
    target_filter: dict | None = None,
    warm_ast: bool = False,
) -> dict[str, int]:
    from libref.services.registry.resolver import resolve_repo

    if explicit_source is not None:
        if version is None:
            raise ValueError("explicit repo source requires a version")
        if ecosystem is not None:
            raise ValueError(
                "explicit source and ecosystem resolution are mutually exclusive"
            )
        eco = "custom"
        vec_store = None
        embed = None
        try:
            from libref.services.infra.vectors import build_vector as _build_vector

            vec_store, embed, _ = _build_vector(settings)
        except Exception:  # noqa: BLE001 — vector stack is optional; indexing continues without it
            vec_store, embed = None, None
        return await index_repo_async(
            conn,
            library.strip(),
            version,
            explicit_source,
            update=update,
            repos_dir=Path(repos_dir)
            if repos_dir is not None
            else Path(settings.repos_dir),
            ecosystem=eco,
            resolved_from="explicit",
            registry_confirmed=True,
            settings=settings,
            vec_store=vec_store,
            embed=embed,
            fresh=fresh,
            subdir=subdir,
            only_files=only_files,
            exclude_patterns=exclude_patterns,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
            heartbeat=heartbeat,
            tenant_id=tenant_id,
            source_role=source_role,
            origin_kind=origin_kind,
            target_filter=target_filter,
            warm_ast=warm_ast,
        )
    async with _client(settings) as client:
        resolution = await resolve_repo(
            conn,
            client,
            settings,
            ecosystem,
            library,
            version,
            include_prerelease=include_prerelease,
        )
    assert resolution.repo_url is not None
    vec_store = None
    embed = None
    try:
        from libref.services.infra.vectors import build_vector as _build_vector

        vec_store, embed, _ = _build_vector(settings)
    except Exception:  # noqa: BLE001 — vector stack is optional; indexing continues without it
        vec_store, embed = None, None
    return await index_repo_async(
        conn,
        resolution.name,
        resolution.version,
        resolution.repo_url,
        update=update,
        repos_dir=Path(repos_dir)
        if repos_dir is not None
        else Path(settings.repos_dir),
        ecosystem=resolution.ecosystem,
        resolved_from=resolution.source,
        registry_confirmed=resolution.registry_confirmed,
        settings=settings,
        vec_store=vec_store,
        embed=embed,
        fresh=fresh,
        subdir=subdir,
        only_files=only_files,
        exclude_patterns=exclude_patterns,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        heartbeat=heartbeat,
        tenant_id=tenant_id,
        source_role=source_role,
        origin_kind=origin_kind,
        target_filter=target_filter,
    )


def build_repo_runner(
    settings, conn, repos_dir=None, vec_store=None, embed=None, mgr=None
):
    """Build a job runner handling repo indexing only; mining lives in mining_refresh."""
    resolved_dir = (
        Path(repos_dir) if repos_dir is not None else Path(settings.repos_dir)
    )

    async def runner(job):
        if job.kind != INDEX_REPO_JOB_KIND:
            raise ValueError(f"unsupported job kind: {job.kind}")
        p = job.params
        heartbeat = (lambda: mgr.heartbeat(job.id)) if mgr is not None else None
        local_vec, local_embed = vec_store, embed
        if local_vec is None or local_embed is None:
            try:
                from libref.services.infra.vectors import build_vector as _build_vector

                built_vec, built_embed, _ = _build_vector(settings)
                if local_vec is None:
                    local_vec = built_vec
                if local_embed is None:
                    local_embed = built_embed
            except Exception:  # noqa: BLE001, S110 — vector stack is optional; indexing continues without it
                pass
        from libref.services.infra.tenant import normalize_tenant as _ntr

        _t = _ntr(
            (p.get("tenant_id") if isinstance(p, dict) else None)
            or getattr(job, "tenant_id", "default")
            or "default"
        )
        return await index_repo_async(
            conn,
            p["library"],
            p["version"],
            p.get("source"),
            update=bool(p.get("update", False)),
            repos_dir=resolved_dir,
            ecosystem=str(p.get("ecosystem", "custom") or "custom"),
            resolved_from=p.get("resolved_from"),
            registry_confirmed=bool(p.get("registry_confirmed", True)),
            repos_quota_mb=int(settings.repos_quota_mb),
            tenant_id=_t,
            settings=settings,
            vec_store=local_vec,
            embed=local_embed,
            fresh=bool(p.get("fresh", False)),
            subdir=p.get("subdir"),
            only_files=p.get("only_files"),
            exclude_patterns=p.get("exclude_patterns"),
            max_files=p.get("max_files"),
            heartbeat=heartbeat,
            source_role=p.get("source_role", "implementation"),
            origin_kind=p.get("origin_kind", "repo_file"),
            target_filter=p.get("target_filter"),
            warm_ast=True,
        )

    return runner
