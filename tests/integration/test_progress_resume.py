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
    load_pending,
    load_progress,
    load_visited,
    resume_offset,
    save_pending,
    save_progress,
    save_vector_watermark,
    save_visited,
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


def test_visited_roundtrip(conn, monkeypatch, tmp_path) -> None:
    _library_id, version_id = seed_library_version(conn)
    assert load_visited(conn, version_id, DOCS_KIND) == []
    save_visited(conn, version_id, DOCS_KIND, {"b", "a"})
    assert load_visited(conn, version_id, DOCS_KIND) == ["a", "b"]
    save_visited(conn, version_id, DOCS_KIND, [])
    assert load_visited(conn, version_id, DOCS_KIND) == []


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


def _write_nested_repo(root) -> None:
    layout = {
        "pkg/__init__.py": "",
        "pkg/core.py": "x = 1\n",
        "pkg/sub/mod.py": "y = 2\n",
        "pkg/sub/deep/leaf.py": "z = 3\n",
        "pkg/tests/t_core.py": "t = 4\n",
        "other/o.py": "w = 5\n",
    }
    for rel, body in layout.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def test_repo_probe_tree_depth_and_exclude(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "nested"
    _write_nested_repo(repo)
    settings = make_settings(monkeypatch, tmp_path)
    shallow = probe_repo(str(repo), settings=settings)
    assert shallow.tree_depth == 1
    assert {e.directory for e in shallow.dir_tree} == {"pkg", "other"}
    deep = probe_repo(str(repo), settings=settings, tree_depth=3)
    by_dir = {e.directory: e.files for e in deep.dir_tree}
    assert by_dir["pkg"] == 5
    assert by_dir["pkg/sub"] == 2
    assert by_dir["pkg/sub/deep"] == 1
    assert by_dir["pkg/tests"] == 1
    assert deep.tree_truncated is False
    scoped = probe_repo(
        str(repo), settings=settings, tree_depth=3, exclude_patterns=[r"(^|/)tests/"]
    )
    assert scoped.exclude_patterns == [r"(^|/)tests/"]
    assert scoped.effective_files == 5
    assert "pkg/tests" not in {e.directory for e in scoped.dir_tree}
    assert scoped.unmatched_only_files == []
    with pytest.raises(ValueError, match="invalid exclude pattern"):
        probe_repo(str(repo), settings=settings, exclude_patterns=["("])
    with pytest.raises(ValueError, match="tree_depth"):
        probe_repo(str(repo), settings=settings, tree_depth=0)


def test_repo_probe_samples_per_dir(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "nested_samples"
    _write_nested_repo(repo)
    settings = make_settings(monkeypatch, tmp_path)
    plain = probe_repo(str(repo), settings=settings)
    assert plain.dir_samples == []
    sampled = probe_repo(
        str(repo), settings=settings, tree_depth=3, samples_per_dir=2
    )
    assert sampled.samples_per_dir == 2
    total = sum(len(entry.files) for entry in sampled.dir_samples)
    assert 0 < total <= settings.probe_sample_limit
    assert all(len(entry.files) <= 2 for entry in sampled.dir_samples)
    by_dir = {entry.directory: entry.files for entry in sampled.dir_samples}
    assert by_dir["pkg"] == ["pkg/__init__.py", "pkg/core.py"]
    assert by_dir["pkg/sub"] == ["pkg/sub/mod.py"]
    assert by_dir["pkg/sub/deep"] == ["pkg/sub/deep/leaf.py"]
    with pytest.raises(ValueError, match="samples_per_dir"):
        probe_repo(str(repo), settings=settings, samples_per_dir=0)
    with pytest.raises(ValueError, match="samples_per_dir"):
        probe_repo(str(repo), settings=settings, samples_per_dir=51)


def test_index_repo_exclude_matches_probe(conn, monkeypatch, tmp_path) -> None:
    from libref.services.code.code_ingest import index_repo

    repo = tmp_path / "nested_index"
    _write_nested_repo(repo)
    settings = make_settings(
        monkeypatch, tmp_path, index_batch_files=10, repos_dir=tmp_path / "repos"
    )
    probe = probe_repo(
        str(repo), settings=settings, exclude_patterns=[r"(^|/)tests/"]
    )
    assert probe.effective_files == 5
    counts = index_repo(
        conn,
        "demo",
        "0.1",
        str(repo),
        repos_dir=tmp_path / "repos",
        settings=settings,
        exclude_patterns=[r"(^|/)tests/"],
    )
    assert counts["files"] == probe.effective_files
    rows = conn.execute(
        "SELECT COUNT(*) FROM call_instances WHERE caller_file LIKE '%tests/%'"
    ).fetchone()
    assert rows[0] == 0
    with pytest.raises(ValueError, match="invalid exclude pattern"):
        index_repo(
            conn,
            "demo",
            "0.2",
            str(repo),
            repos_dir=tmp_path / "repos",
            settings=settings,
            exclude_patterns=["("],
        )


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


def _probe_handler(request: httpx.Request) -> httpx.Response:
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
                f"<url><loc>https://{host}/guide/a</loc></url>"
                f"<url><loc>https://{host}/blog/b</loc></url></urlset>"
            ),
        )
    if path == "/guide/a":
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title>Guide A</title></head><body>"
                '<p><a href="/guide/deep">Deep</a> '
                '<a href="https://x.example/o">X</a></p>'
                "</body></html>"
            ),
        )
    if path == "/blog/b":
        return httpx.Response(
            200,
            headers={"content-type": "text/markdown"},
            text="# Beta\n\nbody\n",
        )
    raise AssertionError(path)


def _patched_client(monkeypatch) -> None:
    import libref.crawler.http as http_mod

    client = httpx.AsyncClient(transport=httpx.MockTransport(_probe_handler))

    @contextlib.asynccontextmanager
    async def fake(_settings):
        yield client

    monkeypatch.setattr(http_mod, "build_async_client", fake)


def test_doc_probe_budget_flag_without_raise(conn, monkeypatch, tmp_path) -> None:
    _patched_client(monkeypatch)
    settings = make_crawler_settings(monkeypatch, tmp_path)
    import asyncio

    over = asyncio.run(
        probe_docs_async(
            conn, settings, "https://d.example/sitemap.xml", max_pages=1
        )
    )
    assert over.budget_exceeded is True
    assert over.accepted == 2
    assert len(over.samples) == 2
    assert over.max_pages == 1
    fitting = asyncio.run(
        probe_docs_async(
            conn, settings, "https://d.example/sitemap.xml", max_pages=5
        )
    )
    assert fitting.budget_exceeded is False
    assert fitting.accepted == 2
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 0


def test_doc_probe_threshold_parity_with_ingest(conn, monkeypatch, tmp_path) -> None:
    _patched_client(monkeypatch)
    settings = make_crawler_settings(monkeypatch, tmp_path)
    import asyncio

    probe = asyncio.run(
        probe_docs_async(
            conn,
            settings,
            "https://d.example/sitemap.xml",
            scope={"crawl_query": "guide", "score_threshold": 0.5},
        )
    )
    assert probe.accepted == 1
    assert probe.samples == ["https://d.example/guide/a"]
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 0


def test_doc_probe_outlink_preview(conn, monkeypatch, tmp_path) -> None:
    _patched_client(monkeypatch)
    settings = make_crawler_settings(monkeypatch, tmp_path)
    import asyncio

    probe = asyncio.run(
        probe_docs_async(conn, settings, "https://d.example/sitemap.xml")
    )
    by_url = {d.url: d for d in probe.sample_details}
    assert by_url["https://d.example/guide/a"].outlink_total == 2
    assert by_url["https://d.example/guide/a"].outlink_external == 1
    assert by_url["https://d.example/guide/a"].external_domains == ["x.example"]
    assert by_url["https://d.example/guide/a"].head_truncated is False
    assert by_url["https://d.example/blog/b"].outlink_total == 0
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 0


def test_doc_probe_follow_flag(conn, monkeypatch, tmp_path) -> None:
    _patched_client(monkeypatch)
    settings = make_crawler_settings(monkeypatch, tmp_path)
    import asyncio

    plain = asyncio.run(
        probe_docs_async(conn, settings, "https://d.example/sitemap.xml")
    )
    assert plain.follow_links is False
    assert plain.follow_expansion_unknown is False
    followed = asyncio.run(
        probe_docs_async(
            conn,
            settings,
            "https://d.example/sitemap.xml",
            scope={"follow_links": True},
        )
    )
    assert followed.follow_links is True
    assert followed.follow_expansion_unknown is True


def test_repo_probe_only_files(conn, monkeypatch, tmp_path) -> None:
    repo = copy_mini_repo(tmp_path)
    settings = make_settings(monkeypatch, tmp_path)
    scoped = probe_repo(str(repo), settings=settings, only_files=["a.py"])
    assert scoped.effective_files == 1
    assert scoped.samples == ["a.py"]
    assert scoped.only_files == ["a.py"]
    assert scoped.unmatched_only_files == []
    partial = probe_repo(
        str(repo), settings=settings, only_files=["a.py", "missing.py"]
    )
    assert partial.effective_files == 1
    assert partial.unmatched_only_files == ["missing.py"]
    with pytest.raises(ValueError):
        probe_repo(str(repo), settings=settings, only_files=[""])
    from libref.crawler.scope import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        probe_repo(
            str(repo),
            settings=settings,
            only_files=["a.py", "b.py"],
            max_files=1,
        )
    assert conn.execute("SELECT count(*) FROM repos").fetchone()[0] == 0


def test_mcp_probe_tool_schemas(conn, monkeypatch, tmp_path) -> None:
    from libref.mcp_server.server import build_mcp_server

    settings = make_settings(monkeypatch, tmp_path)
    mcp = build_mcp_server(conn, settings)
    import asyncio

    tools = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}
    assert {"doc_probe", "repo_probe", "link_stats"} <= set(by_name)
    link_params = by_name["link_stats"].input_schema.get("properties", {})
    assert {"library", "version", "limit", "url"} <= set(link_params)
    repo_params = by_name["repo_probe"].input_schema.get("properties", {})
    assert {"exclude_patterns", "tree_depth", "only_files", "subdir"} <= set(repo_params)
    assert "samples_per_dir" in set(repo_params)
    doc_params = by_name["doc_probe"].input_schema.get("properties", {})
    assert {"max_pages", "prefilter", "scope", "sample_limit"} <= set(doc_params)
    repo_params = by_name["repo_probe"].input_schema.get("properties", {})
    assert {"only_files", "subdir", "max_files", "sample_limit"} <= set(repo_params)


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


def test_pending_depth_roundtrip_and_legacy(conn) -> None:
    _library_id, version_id = seed_library_version(conn)
    assert load_pending(conn, version_id, DOCS_KIND) == []
    save_pending(
        conn,
        version_id,
        DOCS_KIND,
        [("https://d.example/a", 1), ("https://d.example/b", 2)],
    )
    assert load_pending(conn, version_id, DOCS_KIND) == [
        ("https://d.example/a", 1),
        ("https://d.example/b", 2),
    ]
    save_pending(conn, version_id, DOCS_KIND, [])
    assert load_pending(conn, version_id, DOCS_KIND) == []
    conn.execute(
        "UPDATE ingest_progress SET pending=? WHERE version_id=? AND kind=?",
        ('["https://d.example/old"]', version_id, DOCS_KIND),
    )
    conn.commit()
    assert load_pending(conn, version_id, DOCS_KIND) == [("https://d.example/old", 0)]


def test_code_resume_keeps_skipped_call_identities(conn, monkeypatch, tmp_path) -> None:
    from pathlib import Path

    from libref.astminer import config as ast_config
    from libref.astminer.repos import describe_source_files
    from libref.services.code.code_ingest import index_repo

    repo = copy_mini_repo(tmp_path)
    settings = make_settings(
        monkeypatch, tmp_path, index_batch_files=100, repos_dir=tmp_path / "repos"
    )
    full = index_repo(
        conn,
        "demo",
        "0.1",
        str(repo),
        repos_dir=tmp_path / "repos",
        settings=settings,
    )
    assert full["calls"] > 0
    version_id = conn.execute(
        "SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id"
        " WHERE l.name='demo' AND v.version='0.1'"
    ).fetchone()[0]
    expected = sorted(
        r[0]
        for r in conn.execute(
            "SELECT call_uid FROM call_instances WHERE version_id=?", (version_id,)
        ).fetchall()
    )
    assert expected
    local_path = conn.execute(
        "SELECT local_path FROM repos WHERE name=?", ("custom/demo/0.1",)
    ).fetchone()[0]
    census = describe_source_files(Path(local_path), ast_config.all_suffixes())
    files = list(census["files"])
    assert len(files) >= 2
    cut = len(files) // 2
    tail = set(files[cut:])
    placeholders = ",".join("?" for _ in tail)
    conn.execute(
        f"DELETE FROM call_instances WHERE version_id=? AND caller_file IN ({placeholders})",
        (version_id, *sorted(tail)),
    )
    conn.commit()
    save_progress(
        conn,
        version_id,
        CODE_KIND,
        total=len(files),
        done=cut,
        fingerprint=fingerprint_of(files),
        batch_size=1,
    )
    rerun = index_repo(
        conn,
        "demo",
        "0.1",
        str(repo),
        repos_dir=tmp_path / "repos",
        settings=settings,
    )
    assert rerun["resumed_skipped"] == cut
    assert (
        sorted(
            r[0]
            for r in conn.execute(
                "SELECT call_uid FROM call_instances WHERE version_id=?",
                (version_id,),
            ).fetchall()
        )
        == expected
    )


def test_code_resume_replays_batch_lost_before_vector_sync(
    conn, monkeypatch, tmp_path
) -> None:
    from libref.services.code.code_ingest import index_repo
    from libref.services.ops import maintenance as maint
    from tests.conftest import FakeVecStore, fake_embed_vectors

    repo = copy_mini_repo(tmp_path)
    settings = make_settings(
        monkeypatch, tmp_path, index_batch_files=1, repos_dir=tmp_path / "repos"
    )
    vec = FakeVecStore()
    real_sync = maint.sync_vectors_for_docs
    sync_calls: list[int] = []

    async def flaky_sync(*args, **kwargs):
        sync_calls.append(1)
        if len(sync_calls) == 2:
            raise RuntimeError("injected vector sync crash")
        return await real_sync(*args, **kwargs)

    monkeypatch.setattr(maint, "sync_vectors_for_docs", flaky_sync)
    with pytest.raises(RuntimeError, match="injected vector sync crash"):
        index_repo(
            conn,
            "demo",
            "0.1",
            str(repo),
            repos_dir=tmp_path / "repos",
            settings=settings,
            vec_store=vec,
            embed=fake_embed_vectors,
        )
    monkeypatch.setattr(maint, "sync_vectors_for_docs", real_sync)
    rerun = index_repo(
        conn,
        "demo",
        "0.1",
        str(repo),
        repos_dir=tmp_path / "repos",
        settings=settings,
        vec_store=vec,
        embed=fake_embed_vectors,
    )
    assert rerun["resumed_skipped"] == 1
    assert rerun["source_chunks"] > 0
    version_id = conn.execute(
        "SELECT v.id FROM versions v JOIN libraries l ON l.id = v.library_id"
        " WHERE l.name='demo' AND v.version='0.1'"
    ).fetchone()[0]
    want = maint.embeddable_doc_ids(conn, version_id)
    assert want
    have = sorted(
        pid for pid, point in vec.points.items() if point[2] == int(version_id)
    )
    assert have == want
