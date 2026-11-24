"""Source acquisition, file census and declaration collection for indexing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from libref.services.code.repo_collect import _collect_decl_index
from libref.services.code.repo_source import lookup_index_source_cached


def _same_source(left: str | None, right: str | None) -> bool:
    """Source identity ignoring a git prefix and trailing slashes."""

    def _norm(value: str | None) -> str:
        return (value or "").strip().removeprefix("git+").rstrip("/")

    both = (_norm(left), _norm(right))
    return bool(both[0]) and both[0] == both[1]


def prepare_repo_index(
    conn,
    library: str,
    version: str,
    source: str | None,
    *,
    update: bool,
    repos_dir: Path,
    ecosystem: str,
    resolved_from: str | None,
    registry_confirmed: bool,
    repos_quota_mb: int | None,
    settings,
    vec_store,
    embed,
    fresh: bool,
    subdir: str | None,
    only_files: list[str] | None,
    exclude_patterns: list[str] | None,
    max_files: int | None,
    max_file_bytes: int,
    heartbeat,
    tenant_id: str,
    source_role: str,
    origin_kind: str,
    target_filter: dict | None,
    warm_ast: bool,
) -> dict[str, Any]:
    """Resolve the repo, collect source files and stage their declarations."""
    from libref.astminer import cache as ast_cache
    from libref.astminer import config as ast_config
    from libref.astminer.repos import RepoManager, describe_source_files
    from libref.services.infra.progress import CODE_KIND, resume_offset
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
        from libref.astminer import parse as ast_engine

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

    return {
        "library_id": library_id,
        "version_id": version_id,
        "source": source,
        "source_id": source_id,
        "counts_source": counts_source,
        "counts": counts,
        "beat": _beat,
        "repo_name": repo_name,
        "root": root,
        "commit": commit,
        "files": files,
        "batch_size": batch_size,
        "done": done,
        "fingerprint": fingerprint,
        "code_search": code_search,
        "vectors_live": _vectors_live,
        "doc_store": _doc_store,
        "all_decls": all_decls,
        "file_aliases": file_aliases,
        "live": live,
    }
