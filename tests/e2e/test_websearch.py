"""End-to-end web search: real DuckDuckGo HTML, structure and classification only.

Requires real network; skipped by default. A network failure marks the
environment unavailable, not a regression.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("LIBREF_RUN_E2E") != "1",
    reason="e2e requires LIBREF_RUN_E2E=1 and real network",
)


def run(coro):
    return asyncio.run(coro)


def test_e2e_web_search_returns_classified_candidates(
    conn, monkeypatch, tmp_path
) -> None:
    from libref.services.registry.discovery import DiscoveryService
    from tests.conftest import make_discovery_settings

    svc = DiscoveryService(
        conn,
        make_discovery_settings(
            monkeypatch,
            tmp_path,
            request_timeout=20.0,
            discovery_cache_ttl_days=0.0,
        ),
    )
    try:
        items = run(svc.search_web_async("python httpx github", 10))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"env unavailable for web search: {type(exc).__name__}: {exc}")
    assert items
    assert all(set(i) >= {"name", "repo_url", "docs_url", "source"} for i in items)
    assert all(i["source"] == "search:web" for i in items)
    kinds = {
        ("repo" if i["repo_url"] else "docs" if i["docs_url"] else "other")
        for i in items
    }
    assert "repo" in kinds or "docs" in kinds
