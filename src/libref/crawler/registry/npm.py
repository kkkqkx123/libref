"""npm 注册表提供方：名称归一化加负载解析。

归属 registry 子包的 npm 专属部分，通用地址卫生与版本逻辑在子包门面。
新增生态时新增 registry/<eco>.py，并在门面 SUPPORTED_ECOSYSTEMS 追加名称。
"""

from __future__ import annotations

from typing import Any

from libref.crawler.registry import is_repo_host_url, sanitize_http_url


def normalize_npm_name(name: str) -> str:
    return name.strip().lower()


def parse_npm_payload(
    payload: dict[str, Any], *, allow_private: bool = False
) -> tuple[str | None, list[str], str | None, list[str], str | None]:
    tags = payload.get("dist-tags") or {}
    stable = tags.get("latest")
    versions = [str(v) for v in (payload.get("versions") or {})]
    homepage = sanitize_http_url(payload.get("homepage"), allow_private=allow_private)
    candidates = []
    if homepage and not is_repo_host_url(homepage):
        candidates.append(homepage)
    repo_url = None
    repo = payload.get("repository") or {}
    if isinstance(repo, dict):
        repo_url = sanitize_http_url(repo.get("url"), allow_private=allow_private)
    elif isinstance(repo, str):
        repo_url = sanitize_http_url(repo, allow_private=allow_private)
    return stable, versions, homepage, candidates, repo_url


def npm_snapshot_url(base: str, name: str) -> str:
    return f"{base.rstrip('/')}/{name}"
