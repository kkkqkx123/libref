"""注册表人工覆盖：内置表加外部文件合并，精确匹配优先于推导。

内置 OVERRIDES 随代码发布，外部文件经配置追加，文件命中优先于内置，
两者均只低于用户显式地址。过期不降级，由门禁提醒核验。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

# (ecosystem, normalized_name) -> {docs_url, repo_url, source, verified_at}
# docs_url 与 repo_url 至少一项存在，分别服务文档抓取与代码索引。
OVERRIDES: dict[tuple[str, str], dict[str, str]] = {}

_FILE_OVERRIDES: dict[tuple[str, str], dict[str, str]] = {}
_FILE_MTIMES: dict[str, float] = {}


def set_file_overrides(entries: dict[tuple[str, str], dict[str, str]]) -> None:
    _FILE_OVERRIDES.clear()
    _FILE_OVERRIDES.update(entries)


def clear_file_overrides() -> None:
    _FILE_OVERRIDES.clear()
    _FILE_MTIMES.clear()


def all_overrides() -> dict[tuple[str, str], dict[str, str]]:
    merged = dict(OVERRIDES)
    merged.update(_FILE_OVERRIDES)
    return merged


def get_override(ecosystem: str, normalized_name: str) -> dict[str, str] | None:
    """精确匹配覆盖规则，文件优先于内置，命中返回条目，否则返回空。"""
    hit = _FILE_OVERRIDES.get((ecosystem, normalized_name))
    if hit is not None:
        return hit
    return OVERRIDES.get((ecosystem, normalized_name))


def _check_entry(raw: dict) -> tuple[tuple[str, str], dict[str, str]]:
    from libref.crawler.registry import normalize_name

    eco = str(raw.get("ecosystem", "")).strip().lower()
    if eco not in ("pypi", "npm", "maven"):
        raise ValueError(f"unsupported ecosystem in overrides file: {raw!r}")
    norm = normalize_name(eco, str(raw.get("name", "")))
    if not norm:
        raise ValueError(f"empty name in overrides file: {raw!r}")
    docs_url = str(raw.get("docs_url", "") or "").strip()
    repo_url = str(raw.get("repo_url", "") or "").strip()
    if not docs_url and not repo_url:
        raise ValueError(f"override needs docs_url or repo_url: {raw!r}")
    item: dict[str, str] = {}
    if docs_url:
        parts = urlsplit(docs_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"invalid docs_url in overrides file: {raw!r}")
        item["docs_url"] = docs_url
    if repo_url:
        cleaned = repo_url.removeprefix("git+")
        rparts = urlsplit(cleaned)
        if rparts.scheme not in ("http", "https") or not rparts.netloc:
            raise ValueError(f"invalid repo_url in overrides file: {raw!r}")
        item["repo_url"] = repo_url
    verified_at = str(raw.get("verified_at", "")).strip()
    try:
        datetime.strptime(verified_at, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"invalid verified_at in overrides file: {raw!r}") from None
    source = str(raw.get("source", "file")).strip() or "file"
    item.update({"source": source, "verified_at": verified_at})
    return (eco, norm), item


def load_overrides_file(path: str | Path) -> dict[tuple[str, str], dict[str, str]]:
    """读取外部覆盖文件并替换文件层，格式为对象数组的 JSON。"""
    resolved = Path(path)
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TypeError(f"overrides file must hold a list: {resolved}")
    entries: dict[tuple[str, str], dict[str, str]] = {}
    for raw in payload:
        if not isinstance(raw, dict):
            raise TypeError(f"invalid override entry: {raw!r}")
        key, item = _check_entry(raw)
        entries[key] = item
    set_file_overrides(entries)
    try:
        _FILE_MTIMES[str(resolved)] = resolved.stat().st_mtime
    except OSError:
        pass
    return entries


def ensure_overrides_file(path: str | Path) -> None:
    """按修改时间按需重载外部文件，未变化时不重复解析。"""
    resolved = Path(path)
    try:
        mtime = resolved.stat().st_mtime
    except OSError:
        raise ValueError(f"overrides file not found: {resolved}") from None
    if _FILE_MTIMES.get(str(resolved)) == mtime and _FILE_OVERRIDES:
        return
    load_overrides_file(resolved)


def is_override_stale(verified_at: str, ttl_days: float, now: date | None = None) -> bool:
    """核验日期超出有效期或格式非法即视为过期，提示重验。"""
    try:
        verified = datetime.strptime(verified_at.strip(), "%Y-%m-%d").date()
    except ValueError:
        return True
    today = now or date.today()
    return (today - verified).days > ttl_days


def find_stale_overrides(
    ttl_days: float, now: date | None = None
) -> list[tuple[tuple[str, str], dict[str, str]]]:
    """列出全部已过期覆盖，供持续集成门禁失败提醒核验。"""
    return [
        (key, item)
        for key, item in all_overrides().items()
        if is_override_stale(item.get("verified_at", ""), ttl_days, now)
    ]
