"""Registry subpackage facade: models, URL sanitization, version comparison,
transport, and per-ecosystem dispatch.

Ecosystem-specific parsing lives in the provider modules, override rules in
overrides. This facade keeps only dispatch and shared logic; it never reads or
writes storage. All networking goes through an injected httpx.AsyncClient.
Results crossing package boundaries are pydantic models.
To add an ecosystem, add registry/<eco>.py and append its name to SUPPORTED_ECOSYSTEMS.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

SUPPORTED_ECOSYSTEMS = ("pypi", "npm", "maven")

DEFAULT_REGISTRY_BASES = {
    "pypi": "https://pypi.org/pypi",
    "npm": "https://registry.npmjs.org",
    "maven": "https://repo1.maven.org/maven2",
}

# Named whole-group mirror presets; an explicit registry_bases takes per-key precedence over this table.
REGISTRY_MIRRORS = {
    "default": dict(DEFAULT_REGISTRY_BASES),
    "cn": {
        "pypi": "https://pypi.tuna.tsinghua.edu.cn/pypi",
        "npm": "https://registry.npmmirror.com",
        "maven": "https://maven.aliyun.com/repository/public",
    },
}

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
    "canary",
    "experimental",
    "dev",
    "alpha",
    "beta",
    "preview",
    "pre",
    "rc",
    "snapshot",
    "nightly",
)


class RegistryError(Exception):
    """Classified registry query failure: not_found / retryable / no_docs."""

    def __init__(
        self, kind: str, message: str, hint: dict[str, Any] | None = None
    ) -> None:
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
    """Dispatch name normalization per ecosystem; implementations live in provider modules."""
    if ecosystem == "pypi":
        from libref.crawler.registry.pypi import normalize_pypi_name

        return normalize_pypi_name(name)
    if ecosystem == "npm":
        from libref.crawler.registry.npm import normalize_npm_name

        return normalize_npm_name(name)
    if ecosystem == "maven":
        from libref.crawler.registry.maven import normalize_maven_name

        return normalize_maven_name(name)
    raise ValueError(f"unsupported ecosystem: {ecosystem}")


def registry_base_for(ecosystem: str, settings) -> str:
    """Resolve the ecosystem base URL: an explicit mapping wins per-key, else fall back to the preset entry for that ecosystem."""
    if ecosystem not in SUPPORTED_ECOSYSTEMS:
        raise ValueError(f"unsupported ecosystem: {ecosystem}")
    fields_set = getattr(settings, "model_fields_set", set()) or set()
    if "registry_bases" in fields_set:
        bases = dict(getattr(settings, "registry_bases", None) or {})
        if ecosystem in bases and str(bases[ecosystem]).strip():
            return str(bases[ecosystem]).rstrip("/")
    mirror = (
        str(getattr(settings, "registry_mirror", "default") or "default")
        .strip()
        .lower()
    )
    try:
        preset = REGISTRY_MIRRORS[mirror]
    except KeyError:
        available = ", ".join(sorted(REGISTRY_MIRRORS))
        raise ValueError(
            f"unknown registry mirror: {mirror!r} (available: {available})"
        ) from None
    if ecosystem in preset:
        return str(preset[ecosystem]).rstrip("/")
    return str(DEFAULT_REGISTRY_BASES[ecosystem]).rstrip("/")


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
        except IndexError, ValueError:
            pass
    return False


def sanitize_http_url(url: str | None, *, allow_private: bool = False) -> str | None:
    if not url:
        return None
    text = url.strip().strip("<>\"'")
    text = text.removeprefix("git+")
    if text.startswith("git://"):
        text = "https://" + text[len("git://") :]
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
    """Repository root URLs (including in-repo pages) are never used directly as doc candidates."""
    try:
        host = (urlsplit(url).netloc or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _REPO_HOSTS)


def pick_repo_url(
    project_urls: dict[str, str] | None, *, allow_private: bool = False
) -> str | None:
    """Repo hint URL: prefer source-code-style entries, skip funding/sponsorship entries."""
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
    """Fallback when no stable release exists: filter pre-release suffixes, then take the max by version comparison."""
    if not versions:
        return None
    plain = [v for v in versions if not any(h in v.lower() for h in _PRERELEASE_HINTS)]
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


async def _get_text(
    client: httpx.AsyncClient, url: str, *, retries: int = 3
) -> httpx.Response:
    error: str | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.get(
                url, headers={"Accept": "text/xml, application/xml, */*"}
            )
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(min(0.05 * (2**attempt), 0.4))
            continue
        if resp.status_code == 404:
            return resp
        if resp.status_code == 429 or resp.status_code >= 500:
            error = f"HTTP {resp.status_code}"
            await asyncio.sleep(min(0.05 * (2**attempt), 0.4))
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
    elif ecosystem == "npm":
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
    else:
        from libref.crawler.registry.maven import (
            maven_metadata_url,
            maven_pom_url,
            parse_maven_metadata,
            parse_maven_pom,
            split_maven_name,
        )

        base = registry_base_for(ecosystem, settings)
        group, artifact = split_maven_name(name)
        meta_resp = await _get_text(client, maven_metadata_url(base, group, artifact))
        if meta_resp.status_code == 404:
            raise RegistryError("not_found", f"package not found: maven:{name}")
        if meta_resp.status_code >= 400:
            raise RegistryError("retryable", f"registry HTTP {meta_resp.status_code}")
        stable, versions = parse_maven_metadata(meta_resp.text)
        if not versions:
            raise RegistryError("not_found", f"package not found: maven:{name}")
        pom_version = stable or best_effort_stable(versions) or versions[0]
        pom_resp = await _get_text(
            client, maven_pom_url(base, group, artifact, pom_version)
        )
        if pom_resp.status_code >= 400:
            homepage, candidates, repo_url = None, [], None
        else:
            homepage, candidates, repo_url = parse_maven_pom(
                pom_resp.text, allow_private=allow_private
            )
        if stable is not None:
            javadoc_url = f"https://javadoc.io/doc/{group}/{artifact}/{stable}"
            if javadoc_url not in candidates:
                candidates.append(javadoc_url)
    return RegistrySnapshot(
        ecosystem=ecosystem,
        name=name,
        stable=stable,
        versions=versions,
        homepage=homepage,
        doc_candidates=candidates,
        repo_url=repo_url,
    )
