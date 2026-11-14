"""Read-only repository probe: directory shape without parsing any file.

Local paths are walked directly. Remote addresses resolve through
cheapest-first tiers (reused clone, hosting API, blobless git tree, then a
shallow clone kept in the reuse slot); no rows are written to the repos table
and no file is ever parsed. Used by agents and humans to confirm the range
before submitting an index job.
"""

from __future__ import annotations

import time
from pathlib import Path

from pydantic import BaseModel

_PROBE_CACHE: dict[str, tuple[float, RepoProbe]] = {}
_PROBE_CACHE_CAP = 64


class DirGroup(BaseModel):
    directory: str
    count: int


class SuffixGroup(BaseModel):
    suffix: str
    count: int


class RepoProbe(BaseModel):
    source: str
    root_kind: str
    total_candidates: int
    effective_files: int
    skipped: int
    oversized: int
    truncated: bool
    source_note: str = ""
    sizes_known: bool = True
    subdir: str | None = None
    suffix_groups: list[SuffixGroup] = []
    top_dirs: list[DirGroup] = []
    samples: list[str] = []
    manifest_deps: list[dict] = []


def _cache_get(key: str, ttl_hours: float) -> RepoProbe | None:
    hit = _PROBE_CACHE.get(key)
    if hit is None:
        return None
    stored_at, probe = hit
    if ttl_hours <= 0 or time.time() - stored_at > ttl_hours * 3600:
        _PROBE_CACHE.pop(key, None)
        return None
    return probe


def _cache_put(key: str, probe: RepoProbe) -> None:
    while len(_PROBE_CACHE) >= _PROBE_CACHE_CAP:
        oldest = next(iter(_PROBE_CACHE))
        del _PROBE_CACHE[oldest]
    _PROBE_CACHE[key] = (time.time(), probe)


def probe_repo(
    source: str,
    *,
    settings=None,
    subdir: str | None = None,
    max_files: int | None = None,
    sample_limit: int | None = None,
) -> RepoProbe:
    """Walk only; parsing, caching, and persistence never run on this path."""
    from libref.astminer import config as ast_config
    from libref.astminer import gradle as ast_gradle
    from libref.astminer import manifests as ast_manifests
    from libref.astminer.repos import describe_source_files

    if not source.strip():
        raise ValueError("repo source is empty")
    ttl = float(getattr(settings, "probe_cache_ttl_hours", 1.0) or 0.0)
    limit = int(
        sample_limit
        if sample_limit is not None
        else getattr(settings, "probe_sample_limit", 20)
    )
    cache_key = f"{source}\0{subdir or ''}\0{max_files}\0{limit}"
    cached = _cache_get(cache_key, ttl)
    if cached is not None:
        return cached
    maybe_local = Path(source)
    if maybe_local.is_dir():
        root = maybe_local.resolve()
        census = describe_source_files(root, ast_config.all_suffixes(), subdir=subdir)
        probe = _assemble(
            source,
            "local",
            census,
            max_files,
            limit,
            subdir,
            root,
            ast_manifests,
            ast_gradle,
            source_note="local",
            sizes_known=True,
        )
        _cache_put(cache_key, probe)
        return probe
    from libref.services.code.probe_remote import acquire_remote

    try:
        acquired = acquire_remote(
            source,
            settings=settings,
            suffixes=ast_config.all_suffixes(),
            subdir=subdir,
        )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"probe failed for {source}: {exc}") from exc
    if acquired.kind == "root":
        if acquired.root is None:
            raise RuntimeError(f"probe tier returned no data for {source}")
        census = describe_source_files(
            acquired.root, ast_config.all_suffixes(), subdir=subdir
        )
        deps_root = acquired.root
    else:
        if acquired.census is None:
            raise RuntimeError(f"probe tier returned no data for {source}")
        census = acquired.census
        deps_root = None
    probe = _assemble(
        source,
        "remote",
        census,
        max_files,
        limit,
        subdir,
        deps_root,
        ast_manifests,
        ast_gradle,
        source_note=acquired.source_note,
        sizes_known=acquired.sizes_known,
        truncated_extra=acquired.truncated,
    )
    _cache_put(cache_key, probe)
    return probe


def _assemble(
    source,
    kind,
    census,
    max_files,
    limit,
    subdir,
    root,
    manifests,
    gradle,
    source_note="",
    sizes_known=True,
    truncated_extra=False,
) -> RepoProbe:
    files: list[str] = list(census["files"])
    if max_files is not None and len(files) > max_files:
        from libref.crawler.scope import BudgetExceeded

        raise BudgetExceeded(
            f"probe budget exceeded: {len(files)} files over budget {max_files}"
        )
    truncated = truncated_extra
    dir_counts = dict(census["top_dirs"])
    suffix_counts = dict(census["suffix_counts"])
    top_dirs = [
        DirGroup(directory=k, count=v)
        for k, v in sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
    ]
    suffix_groups = [
        SuffixGroup(suffix=k, count=v)
        for k, v in sorted(suffix_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
    ]
    if root is None:
        deps = []
    else:
        try:
            deps = [
                {"ecosystem": d.ecosystem, "name": d.name, "req": d.req}
                for d in manifests.manifest_dependencies(root)
            ]
            deps.extend(
                {"ecosystem": d.ecosystem, "name": d.name, "req": d.req}
                for d in gradle.gradle_dependencies(root)
            )
        except OSError:
            deps = []
    return RepoProbe(
        source=source,
        root_kind=kind,
        total_candidates=len(census["files"])
        + int(census["skipped"])
        + int(census["oversized"]),
        effective_files=len(files),
        skipped=int(census["skipped"]),
        oversized=int(census["oversized"]),
        truncated=truncated,
        subdir=subdir,
        suffix_groups=suffix_groups,
        top_dirs=top_dirs,
        samples=files[: max(0, limit)],
        manifest_deps=deps[:50],
        source_note=source_note,
        sizes_known=sizes_known,
    )
