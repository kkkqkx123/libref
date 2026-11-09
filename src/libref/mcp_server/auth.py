"""MCP/HTTP 鉴权：令牌校验与暴露面硬化，路由保持零业务逻辑。"""

from __future__ import annotations

import ipaddress


def is_loopback_host(host: str) -> bool:
    """判定监听地址是否本地回环，解析失败按对外暴露处理。"""
    name = (host or "").strip().lower()
    if name in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def auth_required(host: str, *, require_auth: bool, has_token: bool) -> bool:
    """对外暴露或显式开启时必须鉴权，本地回环缺令牌时放行。"""
    if require_auth or not is_loopback_host(host):
        return True
    return False


def check_startup(host: str, *, require_auth: bool, token: str | None) -> None:
    """启动前硬化检查：需鉴权而无令牌时拒绝启动，不回落为无鉴权。"""
    if auth_required(host, require_auth=require_auth, has_token=bool(token)):
        if not token:
            raise ValueError(
                f"refusing to expose on {host} without a token: "
                "set LIBREF_MCP_TOKEN or keep the loopback default"
            )


def verify_bearer(authorization: str | None, token: str | None) -> bool:
    """校验 Bearer 令牌，缺任一侧即拒绝，比较采用等长语义避免时序泄漏。"""
    if not authorization or not token:
        return False
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        return False
    if len(credential) != len(token):
        return False
    different = 0
    for left, right in zip(credential, token):
        different |= ord(left) ^ ord(right)
    return different == 0


def is_allowed_origin(origin: str | None, *, host: str) -> bool:
    """来源校验：无来源头放行（非浏览器调用），有来源头须与监听面一致。"""
    if not origin:
        return True
    lowered = origin.strip().lower()
    if is_loopback_host(host):
        return "localhost" in lowered or "127.0.0.1" in lowered or "[::1]" in lowered
    return host.lower() in lowered
