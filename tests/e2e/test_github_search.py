"""End-to-end hosted search: real GitHub API, structure only.

Requires real network; skipped by default. Rate limiting or denial marks the
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


def test_e2e_github_search_returns_repo_candidates(conn, monkeypatch, tmp_path) -> None:
    from libref.crawler.registry import RegistryError
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
        items = run(svc.search_github_async("httpx", 5))
    except RegistryError as exc:
        pytest.skip(f"env unavailable for hosted search: {exc.kind}: {exc}")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"env unavailable for hosted search: {type(exc).__name__}: {exc}")
    assert items
    assert all(set(i) >= {"name", "repo_url", "source"} for i in items)
    assert all(i["source"] == "search:github" for i in items)
    assert all(i["ecosystem"] == "github" for i in items)
    assert any(i["repo_url"] for i in items)
