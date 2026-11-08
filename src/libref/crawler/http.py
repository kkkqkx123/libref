"""抓取链路共享建 client：代理与旁路挂载的唯一收口。

定制点：httpx 显式 proxy 会丢掉环境 NO_PROXY 旁路，
工厂在显式代理下自行合并旁路挂载，保证本地回环不进代理。
测试传输下忽略代理，与 httpx 原生行为一致。
"""

from __future__ import annotations

import ipaddress
import os
from urllib.request import getproxies

import httpx

_DEFAULT_NO_PROXY = "localhost,127.0.0.1,::1"


def _is_ipv4(hostname: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(hostname), ipaddress.IPv4Address)
    except ValueError:
        return False


def _is_ipv6(hostname: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(hostname), ipaddress.IPv6Address)
    except ValueError:
        return False


def proxy_bypass_mounts(no_proxy_text: str | None) -> dict[str, None]:
    """按 httpx 环境旁路规则把 NO_PROXY 文本转为挂载表。"""
    mounts: dict[str, None] = {}
    for raw in (no_proxy_text or "").split(","):
        hostname = raw.strip()
        if not hostname or hostname == "*":
            continue
        if "://" in hostname:
            mounts[hostname] = None
        elif _is_ipv4(hostname):
            mounts[f"all://{hostname}"] = None
        elif _is_ipv6(hostname):
            mounts[f"all://[{hostname}]"] = None
        elif hostname.lower() == "localhost":
            mounts[f"all://{hostname}"] = None
        else:
            mounts[f"all://*{hostname}"] = None
    return mounts


def resolve_proxy_url(settings) -> str | None:
    """显式 LIBREF_HTTP_PROXY 优先，否则读标准环境代理。"""
    explicit = str(getattr(settings, "http_proxy", None) or "").strip()
    if explicit:
        return explicit
    proxies = getproxies()
    for scheme in ("http", "https", "all"):
        if proxies.get(scheme):
            return str(proxies[scheme])
    return None


def resolve_no_proxy_text(settings) -> str:
    """显式 LIBREF_NO_PROXY 优先，否则读环境，否则回环默认。"""
    explicit = str(getattr(settings, "no_proxy", None) or "").strip()
    if explicit:
        return explicit
    env = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if env.strip():
        return env
    return _DEFAULT_NO_PROXY


def proxy_disabled(no_proxy_text: str) -> bool:
    """旁路为全通配时视为停用代理。"""
    return any(raw.strip() == "*" for raw in (no_proxy_text or "").split(","))


def build_async_client(settings, *, transport: httpx.BaseTransport | None = None) -> httpx.AsyncClient:
    """按配置构造抓取用 AsyncClient，调用方以 async with 管理生命周期。"""
    kwargs: dict = {
        "timeout": getattr(settings, "request_timeout", 30.0),
        "follow_redirects": True,
        "headers": {"User-Agent": getattr(settings, "user_agent", "libref-crawler/0.1")},
        "http2": True,
    }
    if transport is not None:
        return httpx.AsyncClient(transport=transport, **kwargs)
    proxy_url = resolve_proxy_url(settings)
    if proxy_url is None:
        return httpx.AsyncClient(**kwargs)
    no_proxy_text = resolve_no_proxy_text(settings)
    if proxy_disabled(no_proxy_text):
        return httpx.AsyncClient(**kwargs)
    return httpx.AsyncClient(
        proxy=proxy_url, mounts=proxy_bypass_mounts(no_proxy_text), **kwargs
    )
