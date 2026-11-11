"""Go module provider: name normalization plus proxy snapshot and pkg.go.dev parsing.

The Go-specific part of the registry subpackage; shared URL sanitization and
version logic live in the subpackage facade. Versions come from the module
proxy; documentation addresses are derived pkg.go.dev module pages.
To add an ecosystem, add registry/<eco>.py and append its name to the facade's SUPPORTED_ECOSYSTEMS.
"""

from __future__ import annotations

from typing import Any

from libref.crawler.registry import is_repo_host_url, sanitize_http_url

GO_PROXY_PATH_HINTS = ("@latest", "@v/list")


def normalize_go_name(name: str) -> str:
    """Module path normalization: trim only; paths are case-sensitive on the proxy."""
    text = name.strip().strip("\"'")
    if not text:
        raise ValueError("empty go module path")
    if any(ch.isspace() for ch in text):
        raise ValueError(f"invalid go module path: {name!r}")
    return text


def go_proxy_path(module: str) -> str:
    """Proxy path escaping: each uppercase letter becomes '!' plus its lowercase form."""
    out: list[str] = []
    for ch in module:
        if ch.isupper():
            out.append("!" + ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def go_latest_url(base: str, module: str) -> str:
    return f"{base.rstrip('/')}/{go_proxy_path(module)}/@latest"


def go_list_url(base: str, module: str) -> str:
    return f"{base.rstrip('/')}/{go_proxy_path(module)}/@v/list"


def parse_go_latest(payload: dict[str, Any]) -> str | None:
    version = payload.get("Version")
    text = str(version or "").strip()
    return text or None


_GO_REPO_HOSTS = (
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "gitee.com",
    "codeberg.org",
)


def infer_go_repo_url(module: str, *, allow_private: bool = False) -> str | None:
    parts = [seg for seg in module.strip().split("/") if seg]
    if len(parts) >= 3 and parts[0].lower() in _GO_REPO_HOSTS:
        return sanitize_http_url(
            f"https://{parts[0]}/{parts[1]}/{parts[2]}", allow_private=allow_private
        )
    return None


def parse_go_repo_url(
    payload: dict[str, Any], module: str, *, allow_private: bool = False
) -> str | None:
    origin = payload.get("Origin") if isinstance(payload, dict) else None
    raw = origin.get("URL") if isinstance(origin, dict) else None
    clean = (
        sanitize_http_url(str(raw), allow_private=allow_private)
        if raw is not None and str(raw).strip()
        else None
    )
    if clean:
        return clean
    return infer_go_repo_url(module, allow_private=allow_private)


def parse_go_list(text: str) -> list[str]:
    """Version list payload: one version per line; blank lines ignored."""
    out: list[str] = []
    for line in (text or "").splitlines():
        version = line.strip()
        if version and version not in out:
            out.append(version)
    return out


def go_docs_url(module: str, version: str | None = None) -> str:
    base = f"https://pkg.go.dev/{module.strip()}"
    if version and version.strip():
        return f"{base}@{version.strip()}"
    return base


def rank_go_candidates(
    module: str, version: str | None, *, allow_private: bool = False
) -> list[str]:
    out: list[str] = []
    clean = sanitize_http_url(go_docs_url(module, version), allow_private=allow_private)
    if clean and not is_repo_host_url(clean):
        out.append(clean)
    return out


PKG_GO_DEV_SEARCH_URL = "https://pkg.go.dev/search"

_NON_MODULE_PATHS = frozenset(
    {
        "/search",
        "/about",
        "/license-policy",
        "/play",
        "/",
    }
)


def go_search_url(query: str) -> str:
    from urllib.parse import urlencode

    return f"{PKG_GO_DEV_SEARCH_URL}?{urlencode({'q': query.strip()})}"


def parse_go_search_html(html: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Extract module paths from pkg.go.dev search result anchors; tolerant to markup drift."""
    from selectolax.lexbor import LexborHTMLParser as HTMLParser

    if not html or not isinstance(html, str):
        return []
    try:
        tree = HTMLParser(html)
    except Exception:  # noqa: BLE001 — malformed markup yields no candidates
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        anchors = list(tree.css("a[href]"))
    except Exception:  # noqa: BLE001 — selector failure yields no candidates
        return []
    for node in anchors:
        try:
            href = str((node.attributes or {}).get("href", "") or "").strip()
        except Exception:  # noqa: BLE001, S112 — unreadable node is skipped
            continue
        if not href.startswith("/") or href in _NON_MODULE_PATHS:
            continue
        if href.startswith(("/search", "/play")):
            continue
        path = href.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        segments = [seg for seg in path.split("/") if seg]
        if len(segments) < 2:
            continue
        name = "/".join(segments)
        if name in seen:
            continue
        seen.add(name)
        out.append(
            {"name": name, "version": "", "description": "", "position": len(out)}
        )
        if len(out) >= max(1, limit):
            break
    return out
