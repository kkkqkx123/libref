"""PyPI 注册表提供方：名称归一化加文档候选排序加负载解析。

归属 registry 子包的 PyPI 专属部分，通用地址卫生与版本逻辑在子包门面。
新增生态时新增 registry/<eco>.py，并在门面 SUPPORTED_ECOSYSTEMS 追加名称。
"""

from __future__ import annotations

from typing import Any

from libref.crawler.registry import (
    is_docs_like_url,
    is_repo_host_url,
    pick_repo_url,
    sanitize_http_url,
)

PYPI_DOCS_KEYS = ("documentation", "docs", "doc", "manual", "guide")


def normalize_pypi_name(name: str) -> str:
    import re

    return re.sub(r"[-_.]+", "-", name.strip()).lower()


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
        if clean and clean not in seen and not is_repo_host_url(clean):
            seen.add(clean)
            out.append(clean)

    lowered = {k.lower(): v for k, v in urls.items()}
    for key in PYPI_DOCS_KEYS:
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


def pypi_snapshot_url(base: str, name: str) -> str:
    return f"{base.rstrip('/')}/{name}/json"
