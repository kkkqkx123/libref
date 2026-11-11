"""End-to-end registry repository verification: six ecosystems return clonable repo addresses.

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

CASES = [
    ("pypi", "httpx", "https://github.com/encode/httpx"),
    ("npm", "express", "https://github.com/expressjs/express"),
    ("crates", "serde", "https://github.com/serde-rs/serde"),
    ("go", "github.com/spf13/cobra", "https://github.com/spf13/cobra"),
    ("maven", "org.springframework:spring-core", "spring-projects/spring-framework"),
    ("nuget", "Newtonsoft.Json", "https://github.com/JamesNK/Newtonsoft.Json"),
]


def run(coro):
    return asyncio.run(coro)


def test_e2e_registry_repo_addresses(conn, monkeypatch, tmp_path) -> None:
    from libref.crawler.http import build_async_client
    from libref.services.registry.resolver import resolve_package, resolve_repo
    from tests.conftest import make_registry_settings

    settings = make_registry_settings(
        monkeypatch,
        tmp_path,
        request_timeout=20.0,
        registry_cache_ttl_days=0.0,
    )

    async def _check() -> list[str]:
        from libref.crawler.registry import is_repo_host_url

        lines: list[str] = []
        async with build_async_client(settings) as client:
            for ecosystem, name, repo_hint in CASES:
                try:
                    resolved = await resolve_package(
                        conn, client, settings, ecosystem, name
                    )
                except Exception as exc:  # noqa: BLE001 — network gaps skip, not regress
                    pytest.skip(f"env unavailable for {ecosystem}:{name}: {exc}")
                assert resolved.version, (ecosystem, name)
                assert resolved.docs_url, (ecosystem, name)
                assert resolved.repo_url, (ecosystem, name)
                assert repo_hint in resolved.repo_url, (
                    ecosystem,
                    name,
                    resolved.repo_url,
                )
                assert is_repo_host_url(resolved.repo_url), (ecosystem, name)
                try:
                    by_repo = await resolve_repo(
                        conn, client, settings, ecosystem, name
                    )
                except Exception as exc:  # noqa: BLE001 — network gaps skip, not regress
                    pytest.skip(f"env unavailable for {ecosystem}:{name}: {exc}")
                assert by_repo.repo_url, (ecosystem, name)
                assert repo_hint in by_repo.repo_url, (ecosystem, name)
                lines.append(
                    f"{ecosystem}:{resolved.name} version={resolved.version} "
                    f"docs={resolved.docs_url} repo={resolved.repo_url}"
                )
        return lines

    lines = run(_check())
    assert len(lines) == len(CASES)
    print("\n".join(lines))
