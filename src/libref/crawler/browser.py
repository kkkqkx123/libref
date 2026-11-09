"""浏览器降级抓取：可选 Playwright 渲染，缺装时显式不可用。

只做按需兜底，不改变静态主链路。调用方按站点开关触发，
渲染失败一律回落为跳过并单独计数，不抛错。
"""

from __future__ import annotations


def is_playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


async def render_html(url: str, *, timeout_ms: int = 15000) -> str | None:
    """渲染页面取 HTML；未安装或失败返回空，由调用方记跳过。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                return await page.content()
            finally:
                await browser.close()
    except Exception:  # noqa: BLE001 —— 渲染失败只回落，不中断整站
        return None
