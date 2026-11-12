"""Grouped ingest, resume cursors, and read-only probes."""

from __future__ import annotations

import contextlib
import types

import httpx
import pytest

from libref.services.code.probe import probe_repo
from libref.services.docs.probe import probe_docs_async
from libref.services.infra.progress import (
    CODE_KIND,
    DOCS_KIND,
    fingerprint_of,
    load_progress,
    resume_offset,
    save_progress,
    save_vector_watermark,
)
from tests.conftest import (
    copy_mini_repo,
    make_crawler_settings,
    make_settings,
    seed_library_version,
)

pytestmark = pytest.mark.integration


def test_resume_offset_fresh_mismatch_done(conn, monkeypatch, tmp_path) -> None:
    _library_id, version_id = seed_library_version(conn)
    items = ["b", "a"]
    done, fingerprint = resume_offset(conn, version_id, DOCS_KIND, items)
    assert (done, fingerprint) == (0, fingerprint_of(items))
    save_progress(
        conn,
        version_id,
        DOCS_KIND,
        total=2,
        done=1,
        fingerprint=fingerprint,
        batch_size=1,
    )
    done, _ = resume_offset(conn, version_id, DOCS_KIND, items)
    assert done == 1
    done, _ = resume_offset(conn, version_id, DOCS_KIND, items, fresh=True)
    assert done == 0
    assert load_progress(conn, version_id, DOCS_KIND) is None
    save_progress(
        conn,
        version_id,
        CODE_KIND,
        total=2,
        done=2,
        fingerprint="stale",
        batch_size=1,
    )
    done, _ = resume_offset(conn, version_id, CODE_KIND, items)
    assert done == 0


def test_plan_targets_explicit_urls() -> None:
    from libref.services.docs.scrape_plan import plan_targets

    seed = types.SimpleNamespace(
        urls=["https://d.example/guide/a", "https://d.example/blog/b"],
        cross_site=[],
        truncated=False,
    )
    scope = {
        "path_prefixes": ["/guide"],
        "urls": ["https://d.example/guide/extra"],
    }
    accepted, rejected, _truncated, _stats = plan_targets(
        seed, "https://d.example/guide/", scope, None
    )
    assert "https://d.example/guide/extra" in accepted
    assert "https://d.example/guide/a" in accepted
    assert "https://d.example/blog/b" in rejected


def test_repo_probe_local(monkeypatch, tmp_path) -> None:
    repo = copy_mini_repo(tmp_path)
    settings = make_settings(monkeypatch, tmp_path)
    probe = probe_repo(str(repo), settings=settings)
    assert probe.effective_files > 0
    assert probe.samples, "probe samples are empty"
    assert probe.suffix_groups, "probe suffix groups are empty"


def test_index_repo_batched_resume_clears_cursor(conn, monkeypatch, tmp_path) -> None:
    from libref.services.code.code_ingest import index_repo

    repo = copy_mini_repo(tmp_path)
    settings = make_settings(
        monkeypatch, tmp_path, index_batch_files=1, repos_dir=tmp_path / "repos"
    )
    counts = index_repo(
        conn,
        "demo",
        "0.1",
        str(repo),
        repos_dir=tmp_path / "repos",
        settings=settings,
    )
    assert counts["entities"] > 0
    assert counts["batches"] >= counts["files"]
    version_id = conn.execute(
        "SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id"
        " WHERE l.name='demo' AND v.version='0.1'"
    ).fetchone()[0]
    assert load_progress(conn, version_id, CODE_KIND) is None
    rerun = index_repo(
        conn,
        "demo",
        "0.1",
        str(repo),
        repos_dir=tmp_path / "repos",
        settings=settings,
        fresh=True,
    )
    assert rerun["entities"] == counts["entities"]


def test_doc_probe_without_storing(conn, monkeypatch, tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(("llms.txt", "llms-full.txt")):
            return httpx.Response(404)
        if path == "/robots.txt":
            return httpx.Response(200, text="")
        if path == "/sitemap.xml":
            host = str(request.url.host)
            return httpx.Response(
                200,
                text=(
                    '<?xml version="1.0"?><urlset>'
                    f"<url><loc>https://{host}/a</loc></url>"
                    f"<url><loc>https://{host}/b</loc></url></urlset>"
                ),
            )
        if path == "/a":
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><head><title>Alpha Guide</title></head><body>hi</body></html>",
            )
        if path == "/b":
            return httpx.Response(
                200,
                headers={"content-type": "text/markdown"},
                text="# Beta\n\nbody\n",
            )
        raise AssertionError(path)

    settings = make_crawler_settings(monkeypatch, tmp_path)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    @contextlib.asynccontextmanager
    async def fake(_settings):
        yield client

    import libref.crawler.http as http_mod

    monkeypatch.setattr(http_mod, "build_async_client", fake)
    import asyncio

    probe = asyncio.run(
        probe_docs_async(conn, settings, "https://d.example/sitemap.xml")
    )
    assert probe.accepted == 2
    assert len(probe.samples) == 2
    assert probe.source == "sitemap"
    assert len(probe.sample_details) == 2
    assert probe.sample_details[0].title == "Alpha Guide"
    assert probe.sample_details[0].content_type == "text/html"
    assert probe.sample_details[1].title == ""
    assert all(d.error == "" for d in probe.sample_details)
    rows = conn.execute("SELECT count(*) FROM pages").fetchone()
    assert rows[0] == 0


def test_vector_watermark_roundtrip(conn) -> None:
    _library_id, version_id = seed_library_version(conn)
    save_progress(
        conn,
        version_id,
        DOCS_KIND,
        total=4,
        done=2,
        fingerprint="fp",
        batch_size=2,
    )
    saved = load_progress(conn, version_id, DOCS_KIND)
    assert saved is not None and saved["vector_done"] == 2
    save_vector_watermark(conn, version_id, DOCS_KIND, 3)
    assert load_progress(conn, version_id, DOCS_KIND)["vector_done"] == 3
    save_progress(
        conn,
        version_id,
        DOCS_KIND,
        total=4,
        done=3,
        fingerprint="fp",
        batch_size=2,
        vector_done=1,
    )
    assert load_progress(conn, version_id, DOCS_KIND)["vector_done"] == 1
