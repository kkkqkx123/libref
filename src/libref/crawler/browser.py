"""Browser fallback crawling: optional Playwright rendering with a reused context.

On-demand fallback only; the static main path is unchanged. Callers trigger it
via per-site switches; render failures always fall back to a skip with a
separate count and never raise.
"""

from __future__ import annotations

import asyncio

_PAGES_BEFORE_RECYCLE = 50

_browser = None
_context = None
_lock: asyncio.Lock | None = None
_pages_since_recycle = 0

_OVERLAY_REMOVAL_JS = """() => {
  try {
    const selectors = [
      '[id*="consent"]', '[class*="consent"]',
      '[id*="cookie"]', '[class*="cookie-banner"]',
      '[id*="overlay"]', '[class*="modal-backdrop"]',
      '[role="dialog"]',
    ];
    for (const sel of selectors) {
      document.querySelectorAll(sel).forEach((el) => {
        if (el && el.parentNode) el.parentNode.removeChild(el);
      });
    }
    document.querySelectorAll('iframe').forEach((frame) => {
      try {
        const doc = frame.contentDocument;
        if (doc && doc.body) {
          const holder = document.createElement('div');
          holder.setAttribute('data-inlined-iframe', frame.src || '');
          holder.innerHTML = doc.body.innerHTML;
          frame.replaceWith(holder);
        }
      } catch (e) { /* cross-origin frames stay untouched */ }
    });
  } catch (e) { /* overlay cleanup is best-effort */ }
}"""


def is_playwright_available() -> bool:
    try:
        import playwright  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


async def render_html(
    url: str, *, timeout_ms: int = 15000, wait_for: str | None = None
) -> str | None:
    """Render the page and return its HTML; return None when not installed or on failure, and the caller records a skip."""
    global _browser, _context, _lock, _pages_since_recycle
    try:
        from playwright.async_api import async_playwright  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        if _lock is None:
            _lock = asyncio.Lock()
        async with _lock:
            if _browser is None or _context is None:
                pw = await async_playwright().start()
                _browser = await pw.chromium.launch()
                _browser._pw = pw  # type: ignore[attr-defined]
                _context = await _browser.new_context()
                _pages_since_recycle = 0
            page = await _context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if wait_for:
                    try:
                        await page.wait_for_selector(wait_for, timeout=timeout_ms)
                    except Exception:  # noqa: BLE001,S110 — missing selector still returns current HTML
                        pass
                try:
                    await page.evaluate(_OVERLAY_REMOVAL_JS)
                except Exception:  # noqa: BLE001,S110 — cleanup is best-effort
                    pass
                html = await page.content()
            finally:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001,S110 — page close failure is ignored
                    pass
            _pages_since_recycle += 1
            if _pages_since_recycle >= _PAGES_BEFORE_RECYCLE:
                await close_browser()
            return html
    except Exception:  # noqa: BLE001 — render failure only falls back, never aborts the whole site
        await close_browser()
        return None


async def close_browser() -> None:
    """Release the reused browser and context; safe to call when nothing is held."""
    global _browser, _context, _pages_since_recycle
    _pages_since_recycle = 0
    if _context is not None:
        try:
            await _context.close()
        except Exception:  # noqa: BLE001,S110 — close is best-effort
            pass
        _context = None
    if _browser is not None:
        pw = getattr(_browser, "_pw", None)
        try:
            await _browser.close()
        except Exception:  # noqa: BLE001,S110 — close is best-effort
            pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:  # noqa: BLE001,S110 — close is best-effort
                pass
        _browser = None
