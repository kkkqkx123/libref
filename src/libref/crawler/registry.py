"""注册表客户端：PyPI 与 npm 只读查询加文档地址排序。

纯网络逻辑，不读写库。网络统一走注入的 httpx.AsyncClient。
跨包边界的结论为 pydantic 模型。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

SUPPORTED_ECOSYSTEMS = ("pypi", "npm")

_DOCS_HOST_HINTS = (
    "docs.",
    "readthedocs",
    "gitbook",
    "mkdocs",
    ".github.io",
    "readthedocs.io",
    "doc.",
    "docs-",
)

_PYPI_DOCS_KEYS = ("documentation", "docs", "doc", "manual", "guide")

_REPO_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "bitbucket.org", "gitee.com")
_FUNDING_KEY_HINTS = ("fund", "sponsor", "donat")
_PRERELEASE_HINTS = (
    "canary", "experimental", "dev", "alpha", "beta", "preview",
    "pre", "rc", "snapshot", "nightly",
)


class RegistryError(Exception):
    """注册表查询分类失败：not_found / retryable / no_docs。"""

    def __init__(self, kind: str, message: str, hint: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.hint = hint or {}


class RegistrySnapshot(BaseModel):
    ecosystem: str
    name: str
    stable: str | None = None
    versions: list[str] = Field(default_factory=list)
    homepage: str | None = None
    doc_candidates: list[str] = Field(default_factory=list)
    repo_url: str | None = None


class Resolution(BaseModel):
    ecosystem: str
    name: str
    requested_version: str | None = None
    version: str
    registry_confirmed: bool = True
    docs_url: str | None = None
    candidates: list[str] = Field(default_factory=list)
    homepage: str | None = None
    repo_url: str | None = None
    source: str = "registry"


def normalize_pypi_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def normalize_npm_name(name: str) -> str:
    return name.strip().lower()


def normalize_name(ecosystem: str, name: str) -> str:
    if ecosystem == "pypi":
        return normalize_pypi_name(name)
    if ecosystem == "npm":
        return normalize_npm_name(name)
    raise ValueError(f"unsupported ecosystem: {ecosystem}")


def _is_private_host(host: str) -> bool:
    h = host.lower().rstrip(".")
    if h in ("localhost", "0.0.0.0", "::1"):
        return True
    if h.endswith((".local", ".internal", ".lan", ".invalid", ".test")):
        return True
    try:
        return ipaddress.ip_address(h).is_private or ipaddress.ip_address(h).is_loopback
    except ValueError:
        pass
    if h.startswith(("127.", "10.", "192.168.")):
        return True
    if h.startswith("172."):
        try:
            second = int(h.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (IndexError, ValueError):
            pass
    return False


def sanitize_http_url(url: str | None, *, allow_private: bool = False) -> str | None:
    if not url:
        return None
    text = url.strip().strip("<>\"'")
    if text.startswith("git+"):
        text = text[4:]
    if text.startswith("git://"):
        text = "https://" + text[len("git://"):]
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.netloc:
        return None
    if not allow_private and _is_private_host(parts.hostname or ""):
        return None
    return text


def is_docs_like_url(url: str) -> bool:
    try:
        host = (urlsplit(url).netloc or "").lower()
    except ValueError:
        return False
    return any(h in host for h in _DOCS_HOST_HINTS)


def _is_repo_host_url(url: str) -> bool:
    """仓库根地址（含仓库内页）永不直接作为文档候选。"""
    try:
        host = (urlsplit(url).netloc or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _REPO_HOSTS)


def pick_repo_url(
    project_urls: dict[str, str] | None, *, allow_private: bool = False
) -> str | None:
    """仓库提示地址：优先源码类条目，跳过赞助类条目。"""
    lowered = {k.lower(): v for k, v in dict(project_urls or {}).items()}

    def clean(value: str | None) -> str | None:
        return sanitize_http_url(value, allow_private=allow_private)

    def is_repo(url: str | None) -> bool:
        return bool(url) and any(h in url for h in _REPO_HOSTS)

    def skipped(key: str) -> bool:
        return any(s in key for s in _FUNDING_KEY_HINTS)

    for prefer in ("source", "code", "repo"):
        for k, v in lowered.items():
            if skipped(k):
                continue
            if prefer in k:
                url = clean(v)
                if is_repo(url):
                    return url
    for k, v in lowered.items():
        if skipped(k):
            continue
        url = clean(v)
        if is_repo(url):
            return url
    return None


def _version_key(value: str) -> list[tuple[int, Any]]:
    key: list[tuple[int, Any]] = []
    for chunk in re.split(r"[.\-+_]", value):
        key.append((0, int(chunk)) if chunk.isdigit() else (1, chunk.lower()))
    return key


def best_effort_stable(versions: list[str]) -> str | None:
    """无稳定版时的兜底：过滤预发布后缀后按版本号比较取最大。"""
    if not versions:
        return None
    plain = [
        v for v in versions
        if not any(h in v.lower() for h in _PRERELEASE_HINTS)
    ]
    return max(plain or versions, key=_version_key)


def rank_pypi_candidates(
    project_urls: dict[str, str] | None,
    home_page: str | None,
    *,
    allow_private: bool = False,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    urls = dict(project_urls or {})

    def push(url: str | None) -> None:
        clean = sanitize_http_url(url, allow_private=allow_private)
        if clean and clean not in seen and not _is_repo_host_url(clean):
            seen.add(clean)
            out.append(clean)

    lowered = {k.lower(): v for k, v in urls.items()}
    for key in _PYPI_DOCS_KEYS:
        for k, v in lowered.items():
            if key in k:
                push(v)
    for k, v in lowered.items():
        flat = k.replace(" ", "").replace("-", "").replace("_", "")
        if "changelog" in flat or "changes" in flat or "release" in flat:
            clean = sanitize_http_url(v, allow_private=allow_private)
            if clean and clean not in seen and is_docs_like_url(clean):
                seen.add(clean)
                out.append(clean)
    for k, v in lowered.items():
        if "home" in k or "site" in k or "web" in k:
            push(v)
    home = sanitize_http_url(home_page, allow_private=allow_private)
    if home and home not in seen and is_docs_like_url(home):
        seen.add(home)
        out.append(home)
    return out


def parse_pypi_payload(
    payload: dict[str, Any], *, allow_private: bool = False
) -> tuple[str | None, list[str], str | None, list[str], str | None]:
    info = payload.get("info") or {}
    stable = info.get("version")
    releases = payload.get("releases") or {}
    versions = [str(v) for v in releases.keys()]
    homepage = sanitize_http_url(info.get("home_page"), allow_private=allow_private)
    candidates = rank_pypi_candidates(
        info.get("project_urls"), info.get("home_page"), allow_private=allow_private
    )
    repo_url = pick_repo_url(info.get("project_urls"), allow_private=allow_private)
    return stable, versions, homepage, candidates, repo_url


def parse_npm_payload(
    payload: dict[str, Any], *, allow_private: bool = False
) -> tuple[str | None, list[str], str | None, list[str], str | None]:
    tags = payload.get("dist-tags") or {}
    stable = tags.get("latest")
    versions = [str(v) for v in (payload.get("versions") or {}).keys()]
    homepage = sanitize_http_url(payload.get("homepage"), allow_private=allow_private)
    candidates = [homepage] if homepage else []
    repo_url = None
    repo = payload.get("repository") or {}
    if isinstance(repo, dict):
        repo_url = sanitize_http_url(repo.get("url"), allow_private=allow_private)
    elif isinstance(repo, str):
        repo_url = sanitize_http_url(repo, allow_private=allow_private)
    return stable, versions, homepage, candidates, repo_url


async def _get_json(
    client: httpx.AsyncClient, url: str, *, retries: int = 3
) -> httpx.Response:
    error: str | None = None
    retry_after = 0.0
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, headers={"Accept": "application/json"})
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(min(0.05 * (2**attempt), 0.4))
            continue
        if resp.status_code == 404:
            return resp
        if resp.status_code == 429 or resp.status_code >= 500:
            error = f"HTTP {resp.status_code}"
            try:
                retry_after = float(resp.headers.get("retry-after", "0"))
            except ValueError:
                retry_after = 0.0
            await asyncio.sleep(min(max(retry_after, 0.05 * (2**attempt)), 0.4))
            continue
        return resp
    raise RegistryError("retryable", f"registry unreachable: {error}")


async def fetch_snapshot(
    ecosystem: str, raw_name: str, client: httpx.AsyncClient, settings
) -> RegistrySnapshot:
    if ecosystem not in SUPPORTED_ECOSYSTEMS:
        raise ValueError(f"unsupported ecosystem: {ecosystem}")
    name = normalize_name(ecosystem, raw_name)
    if not name:
        raise ValueError("empty package name")
    allow_private = bool(getattr(settings, "registry_allow_private", False))
    if ecosystem == "pypi":
        base = getattr(settings, "registry_pypi_base", "https://pypi.org/pypi").rstrip("/")
        resp = await _get_json(client, f"{base}/{name}/json")
        if resp.status_code == 404:
            raise RegistryError("not_found", f"package not found: pypi:{name}")
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"registry HTTP {resp.status_code}")
        stable, versions, homepage, candidates, repo_url = parse_pypi_payload(
            resp.json(), allow_private=allow_private
        )
    else:
        base = getattr(settings, "registry_npm_base", "https://registry.npmjs.org").rstrip("/")
        resp = await _get_json(client, f"{base}/{name}")
        if resp.status_code == 404:
            raise RegistryError("not_found", f"package not found: npm:{name}")
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"registry HTTP {resp.status_code}")
        stable, versions, homepage, candidates, repo_url = parse_npm_payload(
            resp.json(), allow_private=allow_private
        )
    docs_url = candidates[0] if candidates else None
    return RegistrySnapshot(
        ecosystem=ecosystem,
        name=name,
        stable=stable,
        versions=versions,
        homepage=homepage,
        doc_candidates=candidates,
        repo_url=repo_url,
    )
