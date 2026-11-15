"""Remote repository acquisition for probes: cheapest tier first.

A fresh reused clone needs no network at all, hosting APIs return directory
listings without git traffic, a blobless git tree fetch downloads only
directory metadata, and only the last tier does a full shallow clone into the
reuse slot. Every tier degrades to the next on any failure; only the last
tier raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

_API_TIMEOUT_FALLBACK = 10.0
_GITLAB_PAGE_SIZE = 100
_GITLAB_MAX_PAGES = 10
_REUSE_DIRNAME = "probe-reuse"


@dataclass
class RemoteAcquisition:
    """One probe input: a ready census, or a local root left to walk."""

    kind: str  # "census" | "root"
    census: dict | None = None
    root: Path | None = None
    source_note: str = ""
    sizes_known: bool = True
    truncated: bool = False


def _reuse_base(settings) -> Path | None:
    configured = getattr(settings, "repos_dir", None)
    if configured is not None:
        root = Path(configured).parent / _REUSE_DIRNAME
    else:
        root = Path.home() / ".local/share/libref" / _REUSE_DIRNAME
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return root


def _reuse_key(source: str) -> str:
    normalized = source.strip().removeprefix("git+").rstrip("/")
    return hashlib.sha1(normalized.encode("utf-8", "ignore")).hexdigest()


def _slot_fresh(slot: Path, ttl_hours: float) -> bool:
    if ttl_hours <= 0 or not slot.is_dir():
        return False
    try:
        age = time.time() - slot.stat().st_mtime
    except OSError:
        return False
    return age <= ttl_hours * 3600


def _github_target(source: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(source)
    except ValueError:
        return None
    if parts.hostname not in ("github.com", "www.github.com"):
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        return None
    repo = segments[1].removesuffix(".git")
    return segments[0], repo


def _gitlab_target(source: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(source)
    except ValueError:
        return None
    host = parts.hostname or ""
    if not host or host in ("github.com", "www.github.com"):
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        return None
    project = "/".join(segments).removesuffix(".git")
    return host, project


async def _host_get(
    client: httpx.AsyncClient, url: str, *, params: dict | None = None
) -> httpx.Response | None:
    """Host API read with bounded retries; transport and 429/5xx wait and retry."""
    from libref.crawler.http import backoff_delay, compute_retry_delay

    last: httpx.Response | None = None
    for attempt in range(3):
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError, OSError, TimeoutError:
            if attempt >= 2:
                return None
            await asyncio.sleep(backoff_delay(attempt, base=0.05, cap=0.4))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last = resp
            if attempt >= 2:
                return resp
            await asyncio.sleep(compute_retry_delay(resp, attempt, base=0.05, cap=0.4))
            continue
        return resp
    return last


async def _github_census(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    *,
    suffixes,
    subdir,
    exclude_patterns=None,
) -> tuple[dict, bool, bool] | None:
    from libref.astminer.repos import census_of_relpaths

    meta = await _host_get(client, f"/repos/{owner}/{repo}")
    if meta is None or meta.status_code != 200:
        return None
    try:
        branch = meta.json().get("default_branch") or "main"
    except ValueError:
        return None
    tree = await _host_get(
        client, f"/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    )
    if tree is None or tree.status_code != 200:
        return None
    try:
        body = tree.json()
    except ValueError:
        return None
    items: list[tuple[str, int | None]] = []
    for entry in body.get("tree") or []:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            continue
        size = entry.get("size")
        items.append((path, size if isinstance(size, int) else None))
    census = census_of_relpaths(
        items, suffixes=suffixes, subdir=subdir, exclude_patterns=exclude_patterns
    )
    return census, bool(body.get("truncated")), True


async def _gitlab_census(
    host: str, project: str, *, timeout: float, suffixes, subdir, exclude_patterns=None
) -> tuple[dict, bool, bool] | None:
    from libref.astminer.repos import census_of_relpaths

    headers = {"Accept": "application/json", "User-Agent": "libref-probe/0.1"}
    items: list[tuple[str, int | None]] = []
    truncated = False
    async with httpx.AsyncClient(
        base_url=f"https://{host}", timeout=timeout, headers=headers
    ) as client:
        for page in range(1, _GITLAB_MAX_PAGES + 1):
            resp = await _host_get(
                client,
                f"/api/v4/projects/{quote(project, safe='')}/repository/tree",
                params={
                    "recursive": "true",
                    "per_page": str(_GITLAB_PAGE_SIZE),
                    "page": str(page),
                },
            )
            if resp is None or resp.status_code != 200:
                return None
            try:
                entries = resp.json()
            except ValueError:
                return None
            if not entries:
                break
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "blob":
                    continue
                path = entry.get("path")
                if isinstance(path, str) and path:
                    items.append((path, None))
            if len(entries) < _GITLAB_PAGE_SIZE:
                break
        else:
            truncated = True
    census = census_of_relpaths(
        items, suffixes=suffixes, subdir=subdir, exclude_patterns=exclude_patterns
    )
    return census, truncated, False


async def _hosting_census(
    source: str, *, settings, timeout: float, suffixes, subdir, exclude_patterns=None
) -> tuple[dict, bool, bool] | None:
    """Directory listing via hosting API; None when unavailable."""
    try:
        github = _github_target(source)
        if github is not None:
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": "libref-probe/0.1",
            }
            token = getattr(settings, "discovery_github_token", None)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(
                base_url="https://api.github.com", timeout=timeout, headers=headers
            ) as client:
                return await _github_census(
                    client,
                    github[0],
                    github[1],
                    suffixes=suffixes,
                    subdir=subdir,
                    exclude_patterns=exclude_patterns,
                )
        gitlab = _gitlab_target(source)
        if gitlab is not None:
            return await _gitlab_census(
                gitlab[0],
                gitlab[1],
                timeout=timeout,
                suffixes=suffixes,
                subdir=subdir,
                exclude_patterns=exclude_patterns,
            )
        return None
    except Exception:  # noqa: BLE001 — every tier degrades to the next
        return None


def _collect_tree(
    repo, tree_sha, prefix: str, out: list[tuple[str, int | None]]
) -> None:
    tree = repo[tree_sha]
    for entry in tree.items():
        name = entry.path.decode("utf-8", "replace")
        rel = f"{prefix}{name}"
        if entry.mode == 0o160000:
            continue
        if stat.S_ISDIR(entry.mode):
            _collect_tree(repo, entry.sha, rel + "/", out)
        else:
            out.append((rel, None))


def git_tree_census(
    source: str, *, suffixes, subdir, exclude_patterns=None
) -> dict | None:
    """Bare blobless clone plus in-git tree walk; no working tree, no blob downloads."""
    import random as _random
    import time as _time

    from dulwich import porcelain

    tmp = Path(tempfile.mkdtemp(prefix="libref-probe-tree-"))
    try:
        for attempt in range(2):
            try:
                porcelain.clone(
                    source,
                    str(tmp / "repo"),
                    bare=True,
                    depth=1,
                    filter_spec="blob:none",
                )
                break
            except Exception:  # noqa: BLE001 — any fetch failure degrades to the next tier
                if attempt >= 1:
                    return None
                _time.sleep(min(0.5 * (2**attempt), 8.0) + _random.uniform(0.0, 0.1))
                shutil.rmtree(tmp / "repo", ignore_errors=True)
                continue
        try:
            from dulwich.repo import Repo

            repo = Repo(str(tmp / "repo"))
            head_commit: Any = repo[repo.head()]
            out: list[tuple[str, int | None]] = []
            _collect_tree(repo, head_commit.tree, "", out)
        except Exception:  # noqa: BLE001 — unreadable trees degrade to the next tier
            return None
        from libref.astminer.repos import census_of_relpaths

        return census_of_relpaths(
            out, suffixes=suffixes, subdir=subdir, exclude_patterns=exclude_patterns
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def acquire_remote(
    source: str, *, settings, suffixes, subdir, exclude_patterns=None
) -> RemoteAcquisition:
    """Cheapest tier first; raises RuntimeError only when every tier fails."""
    from libref.astminer.repos import describe_source_files

    ttl = float(getattr(settings, "probe_cache_ttl_hours", 1.0) or 0.0)
    base = _reuse_base(settings)
    slot = base / _reuse_key(source) if base is not None else None
    if slot is not None and _slot_fresh(slot, ttl):
        try:
            os.utime(slot, None)
        except OSError:
            pass
        return RemoteAcquisition(kind="root", root=slot, source_note="reused-clone")
    timeout = float(
        getattr(settings, "probe_fetch_timeout", _API_TIMEOUT_FALLBACK)
        or _API_TIMEOUT_FALLBACK
    )
    hosted = asyncio.run(
        _hosting_census(
            source,
            settings=settings,
            timeout=timeout,
            suffixes=suffixes,
            subdir=subdir,
            exclude_patterns=exclude_patterns,
        )
    )
    if hosted is not None:
        census, truncated, sizes_known = hosted
        return RemoteAcquisition(
            kind="census",
            census=census,
            source_note="hosting-api",
            sizes_known=sizes_known,
            truncated=truncated,
        )
    tree_census = git_tree_census(
        source, suffixes=suffixes, subdir=subdir, exclude_patterns=exclude_patterns
    )
    if tree_census is not None:
        return RemoteAcquisition(
            kind="census", census=tree_census, source_note="git-tree", sizes_known=False
        )
    if slot is not None:
        import random as _random
        import time as _time

        from dulwich import porcelain

        last: Exception | None = None
        for attempt in range(3):
            staging_parent = Path(
                tempfile.mkdtemp(prefix="probe-clone-", dir=str(base))
            )
            staging = staging_parent / "repo"
            try:
                porcelain.clone(source, str(staging), depth=1)
            except Exception as exc:  # noqa: BLE001 — probe clone retries then surfaces as RuntimeError
                last = exc
                shutil.rmtree(staging_parent, ignore_errors=True)
                if attempt >= 2:
                    break
                _time.sleep(min(0.5 * (2**attempt), 8.0) + _random.uniform(0.0, 0.1))
                continue
            try:
                shutil.rmtree(slot, ignore_errors=True)
                staging.replace(slot)
            except Exception as exc:
                shutil.rmtree(staging_parent, ignore_errors=True)
                raise RuntimeError(f"probe clone failed for {source}: {exc}") from exc
            shutil.rmtree(staging_parent, ignore_errors=True)
            return RemoteAcquisition(
                kind="root", root=slot, source_note="shallow-clone"
            )
        raise RuntimeError(f"probe clone failed for {source}: {last}") from last
    tmp = Path(tempfile.mkdtemp(prefix="libref-probe-"))
    try:
        from dulwich import porcelain

        try:
            porcelain.clone(source, str(tmp / "repo"), depth=1)
        except Exception as exc:
            raise RuntimeError(f"probe clone failed for {source}: {exc}") from exc
        census = describe_source_files(
            tmp / "repo", suffixes, subdir=subdir, exclude_patterns=exclude_patterns
        )
        return RemoteAcquisition(
            kind="census", census=census, source_note="shallow-clone"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
