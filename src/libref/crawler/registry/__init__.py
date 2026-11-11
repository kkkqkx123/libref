"""Registry subpackage facade: models, URL sanitization, version comparison,
transport, and per-ecosystem dispatch.

Ecosystem-specific parsing lives in the provider modules, override rules in
overrides. This facade keeps only dispatch and shared logic; it never reads or
writes storage. All networking goes through an injected httpx.AsyncClient.
Results crossing package boundaries are pydantic models.
To add an ecosystem, add registry/<eco>.py and append its name to SUPPORTED_ECOSYSTEMS.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

SUPPORTED_ECOSYSTEMS = ("pypi", "npm", "maven", "go", "crates", "nuget")

ECOSYSTEM_ALIASES = {
    "py": "pypi",
    "python": "pypi",
    "pip": "pypi",
    "js": "npm",
    "javascript": "npm",
    "node": "npm",
    "nodejs": "npm",
    "java": "maven",
    "golang": "go",
    "rust": "crates",
    "rs": "crates",
    "cargo": "crates",
    "crates.io": "crates",
    "csharp": "nuget",
    "c#": "nuget",
    "cs": "nuget",
    "dotnet": "nuget",
}


def canonical_ecosystem(ecosystem: str | None) -> str | None:
    """Common language names map to registry ecosystems; canonical names pass through."""
    if ecosystem is None:
        return None
    text = ecosystem.strip().lower()
    if text in SUPPORTED_ECOSYSTEMS:
        return text
    return ECOSYSTEM_ALIASES.get(text)


DEFAULT_REGISTRY_BASES = {
    "pypi": "https://pypi.org/pypi",
    "npm": "https://registry.npmjs.org",
    "maven": "https://repo1.maven.org/maven2",
    "go": "https://proxy.golang.org",
    "crates": "https://crates.io",
    "nuget": "https://api.nuget.org/v3/registration5-gz-semver2",
}

# Named whole-group mirror presets; an explicit registry_bases takes per-key precedence over this table.
# Only mirrors with a verified same-interface address are listed; missing keys fall back to the default base.
REGISTRY_MIRRORS = {
    "default": dict(DEFAULT_REGISTRY_BASES),
    "cn": {
        "pypi": "https://pypi.tuna.tsinghua.edu.cn/pypi",
        "npm": "https://registry.npmmirror.com",
        "maven": "https://maven.aliyun.com/repository/public",
        "go": "https://goproxy.cn",
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
    """Classified registry query failure: not_found / retryable / no_docs / no_repo."""

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
    eco = canonical_ecosystem(ecosystem) or ecosystem.strip().lower()
    if eco == "pypi":
        from libref.crawler.registry.pypi import normalize_pypi_name

        return normalize_pypi_name(name)
    if eco == "npm":
        from libref.crawler.registry.npm import normalize_npm_name

        return normalize_npm_name(name)
    if eco == "maven":
        from libref.crawler.registry.maven import normalize_maven_name

        return normalize_maven_name(name)
    if eco == "go":
        from libref.crawler.registry.go import normalize_go_name

        return normalize_go_name(name)
    if eco == "crates":
        from libref.crawler.registry.crates import normalize_crates_name

        return normalize_crates_name(name)
    if eco == "nuget":
        from libref.crawler.registry.nuget import normalize_nuget_name

        return normalize_nuget_name(name)
    raise ValueError(f"unsupported ecosystem: {ecosystem}")


def registry_base_for(ecosystem: str, settings) -> str:
    """Resolve the ecosystem base URL: an explicit mapping wins per-key, else fall back to the preset entry for that ecosystem."""
    eco = canonical_ecosystem(ecosystem) or ecosystem.strip().lower()
    if eco not in SUPPORTED_ECOSYSTEMS:
        raise ValueError(f"unsupported ecosystem: {ecosystem}")
    fields_set: set[str] = getattr(settings, "model_fields_set", set()) or set()
    if "registry_bases" in fields_set:
        bases = dict(getattr(settings, "registry_bases", None) or {})
        if eco in bases and str(bases[eco]).strip():
            return str(bases[eco]).rstrip("/")
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
    if eco in preset:
        return str(preset[eco]).rstrip("/")
    return str(DEFAULT_REGISTRY_BASES[eco]).rstrip("/")


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
        if not url:
            return False
        return any(h in url for h in _REPO_HOSTS)

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
    """Approximate ordering: split on separator characters and compare digit chunks
    numerically, other chunks case-insensitively. Not PEP 440 or SemVer."""
    key: list[tuple[int, Any]] = []
    for chunk in re.split(r"[.\-+_]", value):
        key.append((0, int(chunk)) if chunk.isdigit() else (1, chunk.lower()))
    return key


def best_effort_stable(
    versions: list[str], *, include_prerelease: bool = False
) -> str | None:
    """Fallback when no stable release exists: filter pre-release suffixes, then take the max by version comparison.

    Approximation: ordering uses the chunk-wise version key, not PEP 440 or
    SemVer; pre-release detection is suffix-hint based, except NuGet which
    treats any hyphenated version as pre-release.
    With inclusion enabled the timeline maximum wins directly, prereleases included."""
    if not versions:
        return None
    if include_prerelease:
        return max(versions, key=_version_key)
    plain = [v for v in versions if not any(h in v.lower() for h in _PRERELEASE_HINTS)]
    return max(plain or versions, key=_version_key)


async def _get_json(
    client: httpx.AsyncClient, url: str, *, retries: int = 3
) -> httpx.Response:
    from libref.crawler.http import resilient_get

    try:
        return await resilient_get(
            client, url, headers={"Accept": "application/json"}, retries=retries
        )
    except httpx.HTTPError as exc:
        raise RegistryError("retryable", f"registry unreachable: {exc}") from None


async def _get_text(
    client: httpx.AsyncClient, url: str, *, retries: int = 3
) -> httpx.Response:
    from libref.crawler.http import resilient_get

    try:
        return await resilient_get(
            client,
            url,
            headers={"Accept": "text/xml, application/xml, */*"},
            retries=retries,
        )
    except httpx.HTTPError as exc:
        raise RegistryError("retryable", f"registry unreachable: {exc}") from None


async def fetch_snapshot(
    ecosystem: str,
    raw_name: str,
    client: httpx.AsyncClient,
    settings,
    include_prerelease: bool = False,
) -> RegistrySnapshot:
    """Registry snapshot: versions always span the full timeline; the stable pick widens to prereleases only when asked."""
    eco = canonical_ecosystem(ecosystem) or ecosystem.strip().lower()
    if eco not in SUPPORTED_ECOSYSTEMS:
        raise ValueError(f"unsupported ecosystem: {ecosystem}")
    name = normalize_name(eco, raw_name)
    if not name:
        raise ValueError("empty package name")
    allow_private = bool(settings.registry_allow_private)
    if eco == "pypi":
        from libref.crawler.registry.pypi import parse_pypi_payload, pypi_snapshot_url

        base = registry_base_for(eco, settings)
        resp = await _get_json(client, pypi_snapshot_url(base, name))
        if resp.status_code == 404:
            raise RegistryError("not_found", f"package not found: pypi:{name}")
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"registry HTTP {resp.status_code}")
        stable, versions, homepage, candidates, repo_url = parse_pypi_payload(
            resp.json(), allow_private=allow_private
        )
        if include_prerelease:
            stable = best_effort_stable(versions, include_prerelease=True) or stable
    elif eco == "npm":
        from libref.crawler.registry.npm import npm_snapshot_url, parse_npm_payload

        base = registry_base_for(eco, settings)
        resp = await _get_json(client, npm_snapshot_url(base, name))
        if resp.status_code == 404:
            raise RegistryError("not_found", f"package not found: npm:{name}")
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"registry HTTP {resp.status_code}")
        stable, versions, homepage, candidates, repo_url = parse_npm_payload(
            resp.json(), allow_private=allow_private
        )
        if include_prerelease:
            stable = best_effort_stable(versions, include_prerelease=True) or stable
    elif eco == "go":
        from libref.crawler.registry.go import (
            go_latest_url,
            go_list_url,
            parse_go_latest,
            parse_go_list,
            parse_go_repo_url,
            rank_go_candidates,
        )

        base = registry_base_for(eco, settings)
        latest_resp = await _get_json(client, go_latest_url(base, name))
        if latest_resp.status_code in (404, 410):
            raise RegistryError("not_found", f"package not found: go:{name}")
        if latest_resp.status_code >= 400:
            raise RegistryError("retryable", f"registry HTTP {latest_resp.status_code}")
        latest_payload = latest_resp.json()
        stable = parse_go_latest(latest_payload)
        list_resp = await _get_text(client, go_list_url(base, name))
        versions = parse_go_list(list_resp.text) if list_resp.status_code < 400 else []
        if stable is not None and stable not in versions:
            versions.append(stable)
        if not versions:
            raise RegistryError("not_found", f"package not found: go:{name}")
        if include_prerelease:
            stable = best_effort_stable(versions, include_prerelease=True) or stable
        homepage, candidates, repo_url = (
            None,
            rank_go_candidates(name, stable, allow_private=allow_private),
            parse_go_repo_url(latest_payload, name, allow_private=allow_private),
        )
    elif eco == "crates":
        from libref.crawler.registry.crates import (
            crates_snapshot_url,
            parse_crates_snapshot,
        )

        base = registry_base_for(eco, settings)
        resp = await _get_json(client, crates_snapshot_url(base, name))
        if resp.status_code == 404:
            raise RegistryError("not_found", f"package not found: crates:{name}")
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"registry HTTP {resp.status_code}")
        stable, versions, homepage, candidates, repo_url = parse_crates_snapshot(
            resp.json(),
            allow_private=allow_private,
            include_prerelease=include_prerelease,
        )
        if not versions:
            raise RegistryError("not_found", f"package not found: crates:{name}")
    elif eco == "nuget":
        from libref.crawler.registry.nuget import (
            nuget_nuspec_url,
            nuget_page_urls,
            nuget_repo_from_project_url,
            nuget_snapshot_url,
            parse_nuget_nuspec,
            parse_nuget_registration,
            pick_nuget_stable,
            rank_nuget_candidates,
        )

        base = registry_base_for(eco, settings)
        resp = await _get_json(client, nuget_snapshot_url(base, name))
        if resp.status_code == 404:
            raise RegistryError("not_found", f"package not found: nuget:{name}")
        if resp.status_code >= 400:
            raise RegistryError("retryable", f"registry HTTP {resp.status_code}")
        payload = resp.json()
        versions, homepage = parse_nuget_registration(
            payload, allow_private=allow_private
        )
        for page_url in nuget_page_urls(payload):
            page_resp = await _get_json(client, page_url)
            if page_resp.status_code >= 400:
                continue
            page_versions, page_home = parse_nuget_registration(
                page_resp.json(), allow_private=allow_private
            )
            for version in page_versions:
                if version not in versions:
                    versions.append(version)
            if homepage is None:
                homepage = page_home
        if not versions:
            raise RegistryError("not_found", f"package not found: nuget:{name}")
        stable = pick_nuget_stable(versions, include_prerelease=include_prerelease)
        candidates = rank_nuget_candidates(
            homepage, name, stable, allow_private=allow_private
        )
        repo_url = nuget_repo_from_project_url(homepage, allow_private=allow_private)
        if stable is not None and base.rstrip("/") == DEFAULT_REGISTRY_BASES["nuget"]:
            try:
                nuspec_resp = await _get_text(client, nuget_nuspec_url(name, stable))
            except Exception:  # noqa: BLE001 — auxiliary repository lookup never blocks resolution
                nuspec_resp = None
            if nuspec_resp is not None and nuspec_resp.status_code < 400:
                nuspec_repo = parse_nuget_nuspec(
                    nuspec_resp.text, allow_private=allow_private
                )
                if nuspec_repo is not None:
                    repo_url = nuspec_repo
    else:
        from libref.crawler.registry.maven import (
            maven_metadata_url,
            maven_pom_url,
            parse_maven_metadata,
            parse_maven_pom,
            split_maven_name,
        )

        base = registry_base_for(eco, settings)
        group, artifact = split_maven_name(name)
        meta_resp = await _get_text(client, maven_metadata_url(base, group, artifact))
        if meta_resp.status_code == 404:
            raise RegistryError("not_found", f"package not found: maven:{name}")
        if meta_resp.status_code >= 400:
            raise RegistryError("retryable", f"registry HTTP {meta_resp.status_code}")
        stable, versions = parse_maven_metadata(meta_resp.text)
        if not versions:
            raise RegistryError("not_found", f"package not found: maven:{name}")
        if include_prerelease:
            stable = best_effort_stable(versions, include_prerelease=True) or stable
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
        ecosystem=eco,
        name=name,
        stable=stable,
        versions=versions,
        homepage=homepage,
        doc_candidates=candidates,
        repo_url=repo_url,
    )
