"""注册表子包门面：模型、地址卫生、版本比较、传输加生态分发。

生态专属解析落 pypi 与 npm 提供方，覆盖规则落 overrides，
本门面只保留分发与通用逻辑，不读写库。
网络统一走注入的 httpx.AsyncClient。
跨包边界的结论为 pydantic 模型。
新增生态时新增 registry/<eco>.py 并在 SUPPORTED_ECOSYSTEMS 追加名称。
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


def normalize_name(ecosystem: str, name: str) -> str:
    """按生态分发归一化，实现落各提供方模块。"""
    if ecosystem == "pypi":
        from libref.crawler.registry.pypi import normalize_pypi_name

        return normalize_pypi_name(name)
    if ecosystem == "npm":
        from libref.crawler.registry.npm import normalize_npm_name

        return normalize_npm_name(name)
    raise ValueError(f"unsupported ecosystem: {ecosystem}")


def registry_base_for(ecosystem: str, settings) -> str:
    """解析生态基址：registry_bases 映射表为唯一来源。"""
    bases = settings.registry_bases
    try:
        return str(bases[ecosystem]).rstrip("/")
    except KeyError:
        raise ValueError(f"unsupported ecosystem: {ecosystem}") from None


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
    text = text.removeprefix("git+")
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


def is_repo_host_url(url: str) -> bool:
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
    allow_private = bool(settings.registry_allow_private)
    if ecosystem == "pypi":
        from libref.crawler.registry.pypi import parse_pypi_payload, pypi_snapshot_url

        base = registry_base_for(ecosystem, settings)
        resp = await _get_json(client, pypi_snapshot_url(base, name))
        if resp.status_code == 404:
            raise RegistryError("not_found", f"package not found: pypi:{name}")
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"registry HTTP {resp.status_code}")
        stable, versions, homepage, candidates, repo_url = parse_pypi_payload(
            resp.json(), allow_private=allow_private
        )
    else:
        from libref.crawler.registry.npm import npm_snapshot_url, parse_npm_payload

        base = registry_base_for(ecosystem, settings)
        resp = await _get_json(client, npm_snapshot_url(base, name))
        if resp.status_code == 404:
            raise RegistryError("not_found", f"package not found: npm:{name}")
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"registry HTTP {resp.status_code}")
        stable, versions, homepage, candidates, repo_url = parse_npm_payload(
            resp.json(), allow_private=allow_private
        )
    return RegistrySnapshot(
        ecosystem=ecosystem,
        name=name,
        stable=stable,
        versions=versions,
        homepage=homepage,
        doc_candidates=candidates,
        repo_url=repo_url,
    )
