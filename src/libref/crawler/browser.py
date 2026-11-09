"""Browser fallback crawling: optional Playwright rendering, explicitly unavailable when not installed.

On-demand fallback only; the static main path is unchanged. Callers trigger it
via per-site switches; render failures always fall back to a skip with a
separate count and never raise.
"""

from __future__ import annotations


def is_playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


async def render_html(url: str, *, timeout_ms: int = 15000) -> str | None:
    """Render the page and return its HTML; return None when not installed or on failure, and the caller records a skip."""
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
    except Exception:  # noqa: BLE001 — render failure only falls back, never aborts the whole site
        return None
