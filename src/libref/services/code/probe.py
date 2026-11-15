"""Read-only repository probe: directory shape without parsing any file.

Local paths are walked directly. Remote addresses resolve through
cheapest-first tiers (reused clone, hosting API, blobless git tree, then a
shallow clone kept in the reuse slot); no rows are written to the repos table
and no file is ever parsed. Used by agents and humans to confirm the range
before submitting an index job.
"""

from __future__ import annotations

import json as _json
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


class DirEntry(BaseModel):
    directory: str
    files: int


class DirSamples(BaseModel):
    directory: str
    files: list[str] = []


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
    only_files: list[str] | None = None
    unmatched_only_files: list[str] = []
    exclude_patterns: list[str] | None = None
    tree_depth: int = 1
    dir_tree: list[DirEntry] = []
    tree_truncated: bool = False
    samples_per_dir: int | None = None
    dir_samples: list[DirSamples] = []
    suffix_groups: list[SuffixGroup] = []
    top_dirs: list[DirGroup] = []
    samples: list[str] = []
    manifest_deps: list[dict] = []


def _normalize_source(source: str) -> str:
    return source.strip().removeprefix("git+").rstrip("/")


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
    only_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    tree_depth: int | None = None,
    samples_per_dir: int | None = None,
) -> RepoProbe:
    """Walk only; parsing, caching, and persistence never run on this path."""
    from libref.astminer import config as ast_config
    from libref.astminer import gradle as ast_gradle
    from libref.astminer import manifests as ast_manifests
    from libref.astminer.repos import describe_source_files

    if not source.strip():
        raise ValueError("repo source is empty")
    normalized = _normalize_source(source)
    only_list = list(only_files) if only_files is not None else None
    if only_list is not None:
        for item in only_list:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("only_files must be non-empty strings")
    exclude_list = list(exclude_patterns) if exclude_patterns is not None else None
    if exclude_list is not None:
        for item in exclude_list:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("exclude_patterns must be non-empty strings")
        try:
            import re as _re

            for item in exclude_list:
                _re.compile(item)
        except _re.error as exc:
            raise ValueError(f"invalid exclude pattern: {exc}") from exc
    depth = 1 if tree_depth is None else tree_depth
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 1:
        raise ValueError("tree_depth must be a positive integer")
    if samples_per_dir is not None and (
        not isinstance(samples_per_dir, int)
        or isinstance(samples_per_dir, bool)
        or samples_per_dir < 1
        or samples_per_dir > 50
    ):
        raise ValueError("samples_per_dir must be an integer between 1 and 50")
    ttl = float(getattr(settings, "probe_cache_ttl_hours", 1.0) or 0.0)
    limit = int(
        sample_limit
        if sample_limit is not None
        else getattr(settings, "probe_sample_limit", 20)
    )
    try:
        only_text = _json.dumps(
            only_list, sort_keys=True, ensure_ascii=False, default=str
        )
        exclude_text = _json.dumps(
            exclude_list, sort_keys=True, ensure_ascii=False, default=str
        )
    except TypeError, ValueError:
        only_text = str(only_list)
        exclude_text = str(exclude_list)
    cache_key = "\0".join(
        (
            normalized,
            subdir or "",
            str(max_files),
            str(limit),
            only_text,
            exclude_text,
            str(depth),
            str(samples_per_dir),
        )
    )
    cached = _cache_get(cache_key, ttl)
    if cached is not None:
        return cached
    maybe_local = Path(source)
    if maybe_local.is_dir():
        root = maybe_local.resolve()
        census = describe_source_files(
            root,
            ast_config.all_suffixes(),
            subdir=subdir,
            exclude_patterns=exclude_list,
        )
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
            only_files=only_list,
            exclude_patterns=exclude_list,
            tree_depth=depth,
            samples_per_dir=samples_per_dir,
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
            exclude_patterns=exclude_list,
        )
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"probe failed for {source}: {exc}") from exc
    if acquired.kind == "root":
        if acquired.root is None:
            raise RuntimeError(f"probe tier returned no data for {source}")
        census = describe_source_files(
            acquired.root,
            ast_config.all_suffixes(),
            subdir=subdir,
            exclude_patterns=exclude_list,
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
        only_files=only_list,
        exclude_patterns=exclude_list,
        tree_depth=depth,
        samples_per_dir=samples_per_dir,
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
    only_files: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    tree_depth: int = 1,
    samples_per_dir: int | None = None,
) -> RepoProbe:
    files: list[str] = list(census["files"])
    unmatched: list[str] = []
    if only_files is not None:
        wanted = set(only_files)
        unmatched = [item for item in only_files if item not in set(files)]
        files = [item for item in files if item in wanted]
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
    base = (subdir or "").strip().strip("/")
    base_depth = len(base.split("/")) if base else 0
    tree_items = [
        (path, count)
        for path, count in dict(census.get("dir_files", {})).items()
        if len(path.split("/")) - base_depth <= tree_depth
        and (not base or path == base or path.startswith(base + "/"))
    ]
    tree_items.sort(key=lambda kv: (-kv[1], kv[0]))
    tree_truncated = len(tree_items) > 100
    dir_tree = [
        DirEntry(directory=path, files=count) for path, count in tree_items[:100]
    ]
    dir_samples: list[DirSamples] = []
    if samples_per_dir is not None:
        budget = max(0, int(limit))
        by_parent: dict[str, list[str]] = {}
        for path in files:
            by_parent.setdefault(path.rpartition("/")[0] or "(root)", []).append(path)
        ordered = [entry.directory for entry in dir_tree]
        leftovers = sorted(
            (d for d in by_parent if d not in set(ordered)),
            key=lambda d: (-len(by_parent[d]), d),
        )
        used = 0
        for directory in ordered + leftovers:
            if used >= budget:
                break
            chunk = by_parent.get(directory, [])[:samples_per_dir]
            chunk = chunk[: budget - used]
            if chunk:
                dir_samples.append(DirSamples(directory=directory, files=chunk))
                used += len(chunk)
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
        only_files=list(only_files) if only_files is not None else None,
        unmatched_only_files=unmatched,
        exclude_patterns=list(exclude_patterns)
        if exclude_patterns is not None
        else None,
        tree_depth=tree_depth,
        dir_tree=dir_tree,
        tree_truncated=tree_truncated,
        samples_per_dir=samples_per_dir,
        dir_samples=dir_samples,
        suffix_groups=suffix_groups,
        top_dirs=top_dirs,
        samples=files[: max(0, limit)],
        manifest_deps=deps[:50],
        source_note=source_note,
        sizes_known=sizes_known,
    )
