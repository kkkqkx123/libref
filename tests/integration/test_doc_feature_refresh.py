"""Persistence, idempotence, read models, and filtering for document features."""

from __future__ import annotations

import json

import pytest

from libref.services.docs.doc_insight import DocInsightService
from libref.services.docs.feature_refresh import refresh_doc_features
from libref.services.infra.jobs import DOC_FEATURE_KIND
from libref.services.registry.libraries import LibraryService
from libref.store.retriever import filter_by_doc_facets
from tests.conftest import make_settings

pytestmark = pytest.mark.integration

_URL = "https://docs.example/client"


def _seed(conn, ecosystem="custom"):
    catalog = LibraryService(conn)
    library_id, version_id = catalog.ensure_library_version("demo", "1.0", ecosystem)
    conn.execute("INSERT INTO pages(version_id, url) VALUES(?, ?)", (version_id, _URL))
    page_id = conn.execute(
        "SELECT id FROM pages WHERE version_id=? AND url=?", (version_id, _URL)
    ).fetchone()[0]
    metadata = json.dumps(
        {"title": "Client", "path": "Client / Examples", "chunk_path": "embedding"}
    )
    conn.execute(
        "INSERT INTO documents(library_id, version_id, page_id, url, content, metadata, sort_order)"
        " VALUES(?,?,?,?,?,?,0)",
        (
            library_id,
            version_id,
            page_id,
            _URL,
            "# Examples\n\n```python\nclient.connect()\nclient.close()\n```\n",
            metadata,
        ),
    )
    doc_id = conn.execute(
        "SELECT id FROM documents WHERE page_id=?", (page_id,)
    ).fetchone()[0]
    for entity_id, name in ((1, "demo.Client.connect"), (2, "demo.Client.close")):
        conn.execute(
            "INSERT INTO api_entities(entity_uid, library_id, version_id, qualified_name, kind)"
            " VALUES(?,?,?,?,'function')",
            (f"uid-{entity_id}", library_id, version_id, name),
        )
    for order, entity_id, name in (
        (1, 1, "demo.Client.connect"),
        (2, 2, "demo.Client.close"),
    ):
        conn.execute(
            "INSERT INTO call_instances(call_uid, callee_entity_id, callee_name, args,"
            " repo, caller_file, line_start, line_end, confidence, library_id, version_id,"
            " sample_id, call_order, source_role, origin_kind, target_status)"
            " VALUES(?,?,?,'[]','docs',?,1,2,'EXTRACTED',?,?,?,?,'doc_snippet',"
            " 'markdown_fence','resolved_target')",
            (
                f"call-{entity_id}",
                entity_id,
                name,
                _URL,
                library_id,
                version_id,
                f"doc:{version_id}:{_URL}#1",
                order,
            ),
        )
    from libref.services.code.usage_sources import resolve_profile

    profile = resolve_profile(conn, version_id=version_id, tenant_id="default")
    conn.execute(
        "INSERT INTO seq_patterns(library_id, version_id, profile_id, pattern, support, is_closed)"
        " VALUES(?,?,?,?,?,1)",
        (
            library_id,
            version_id,
            profile["id"],
            json.dumps(["demo.Client.connect", "demo.Client.close"]),
            3,
        ),
    )
    for path, parent, depth in (
        ("demo", "", 1),
        ("demo.Client", "demo", 2),
        ("demo.Client.connect", "demo.Client", 3),
        ("demo.Client.close", "demo.Client", 3),
    ):
        conn.execute(
            "INSERT INTO feature_nodes(library_id, version_id, profile_id, path,"
            " parent, depth, kind, calls, scopes)"
            " VALUES(?,?,?,?,?,?,'function',2,1)",
            (library_id, version_id, profile["id"], path, parent, depth),
        )
    conn.commit()
    return library_id, version_id, doc_id


def test_refresh_backfills_entities_and_is_idempotent(conn, monkeypatch, tmp_path):
    _library_id, version_id, doc_id = _seed(conn)
    settings = make_settings(monkeypatch, tmp_path)
    first = refresh_doc_features(conn, "demo", "1.0", settings=settings)
    assert first["features"] > 0
    assert first["links"] == 2
    assert first["entities_updated"] == 1
    assert first["coverage"] == 1
    assert first["consistency"] == 1
    assert first["nodes"] == 6

    links = conn.execute(
        "SELECT ae.qualified_name FROM doc_api_links l"
        " JOIN api_entities ae ON ae.id=l.api_entity_id"
        " WHERE l.version_id=? ORDER BY ae.qualified_name",
        (version_id,),
    ).fetchall()
    assert [row[0] for row in links] == ["demo.Client.close", "demo.Client.connect"]
    meta = json.loads(
        conn.execute("SELECT metadata FROM documents WHERE id=?", (doc_id,)).fetchone()[
            0
        ]
    )
    assert meta["entities"] == ["demo.Client.close", "demo.Client.connect"]
    fts = conn.execute(
        "SELECT count(*) FROM documents_fts WHERE documents_fts MATCH 'connect'"
    ).fetchone()[0]
    assert fts >= 1

    before = conn.execute(
        "SELECT facet, value FROM doc_features WHERE version_id=? ORDER BY facet",
        (version_id,),
    ).fetchall()
    second = refresh_doc_features(conn, "demo", "1.0", settings=settings)
    for key in (
        "features",
        "links",
        "coverage",
        "consistency",
        "nodes",
        "dropped",
    ):
        assert second[key] == first[key]
    assert second["entities_updated"] == 0
    after = conn.execute(
        "SELECT facet, value FROM doc_features WHERE version_id=? ORDER BY facet",
        (version_id,),
    ).fetchall()
    assert before == after


def test_insight_views_and_filters(conn, monkeypatch, tmp_path):
    _library_id, version_id, doc_id = _seed(conn)
    settings = make_settings(monkeypatch, tmp_path)
    refresh_doc_features(conn, "demo", "1.0", settings=settings)
    svc = DocInsightService(conn, settings)
    assert svc.insight("demo", "1.0", view="features", limit=100)
    links = svc.insight("demo", "1.0", view="links", limit=100)
    assert len(links) == 2
    assert all(row["feature_nodes"][0] == "demo" for row in links)
    assert len(svc.insight("demo", "1.0", view="links", document_id=doc_id)) == 2
    assert svc.insight("demo", "1.0", view="links", document_id=doc_id + 999) == []
    assert svc.insight("demo", "1.0", view="coverage", limit=100)
    verdicts = svc.insight("demo", "1.0", view="consistency", limit=100)
    assert verdicts and all(row["note"] == "" for row in verdicts)
    assert svc.compare_versions("demo", "1.0", "1.0") == []

    from libref.store.retriever import _doc_feature_bonuses

    bonuses = _doc_feature_bonuses(conn, [version_id])
    assert bonuses[version_id][doc_id] > 0

    kept = filter_by_doc_facets(
        conn, [doc_id], block_type="example", exclude_deprecated=True, aligned_only=True
    )
    assert kept == [doc_id]
    assert filter_by_doc_facets(conn, [doc_id], block_type="warning") == []


def test_disabled_derivation_writes_nothing(conn, monkeypatch, tmp_path):
    _seed(conn)
    settings = make_settings(monkeypatch, tmp_path, doc_feature_enabled=False)
    counts = refresh_doc_features(conn, "demo", "1.0", settings=settings)
    assert counts["features"] == 0
    assert conn.execute("SELECT count(*) FROM doc_features").fetchone()[0] == 0


def test_mining_refresh_keeps_document_ecosystem(conn, monkeypatch):
    import libref.services.docs.feature_refresh as doc_features
    from libref.services.code.mining_refresh import _refresh_doc_derived

    seen: list[str | None] = []
    original = doc_features.refresh_doc_features

    def capture(conn, *args, **kwargs):
        seen.append(kwargs.get("ecosystem"))
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(doc_features, "refresh_doc_features", capture)
    _seed(conn, ecosystem="pypi")
    _refresh_doc_derived(conn, "demo", "1.0", None, "default", "pypi")

    assert seen == ["pypi"]
    libraries = conn.execute(
        "SELECT ecosystem FROM libraries WHERE name='demo'"
    ).fetchall()
    assert [row[0] for row in libraries] == ["pypi"]


def test_mine_patterns_does_not_add_library_or_version(conn):
    from libref.services.code.mining_refresh import mine_patterns

    _seed(conn, ecosystem="pypi")
    before_libraries = conn.execute("SELECT count(*) FROM libraries").fetchone()[0]
    before_versions = conn.execute("SELECT count(*) FROM versions").fetchone()[0]
    mine_patterns(conn, "demo", "1.0", ecosystem="pypi")
    after_libraries = conn.execute("SELECT count(*) FROM libraries").fetchone()[0]
    after_versions = conn.execute("SELECT count(*) FROM versions").fetchone()[0]
    assert after_libraries == before_libraries
    assert after_versions == before_versions
    ecosystems = conn.execute(
        "SELECT ecosystem FROM libraries WHERE name='demo'"
    ).fetchall()
    assert [row[0] for row in ecosystems] == ["pypi"]


def test_entity_source_binding_resolves_snippets_without_copying_entities(conn):
    from libref.astminer.models import CodeSnippet
    from libref.services.code.usage_ingest import ingest_snippets

    catalog = LibraryService(conn)
    _library_id, source_id = catalog.ensure_library_version("bound", "1.0", "go")
    cursor = conn.execute(
        "INSERT INTO api_entities(entity_uid, library_id, version_id, qualified_name, kind)"
        " VALUES('bound-source', ?, ?, 'example.com.demo.Client.Connect', 'method')",
        (_library_id, source_id),
    )
    entity_id = int(cursor.lastrowid)
    _library_id, doc_id = catalog.ensure_library_version("bound", "docs", "go")
    catalog.bind_entity_source("bound", "docs", "1.0", ecosystem="go")
    assert catalog.entity_source_version_id(doc_id) == source_id
    snippet = CodeSnippet(
        sample_id="doc#1",
        language="go",
        code="demo.Client.Connect()",
        import_context='import "example.com/demo"',
    )
    ingest_snippets(
        conn,
        "bound",
        "docs",
        [snippet],
        source_key="doc:bound",
        ecosystem="go",
        target_filter={"prefixes": ["example.com.demo"]},
    )
    row = conn.execute(
        "SELECT callee_entity_id, callee_name FROM call_instances WHERE version_id=?",
        (doc_id,),
    ).fetchone()
    assert row is not None
    assert int(row[0]) == entity_id
    assert row[1] == "example.com.demo.Client.Connect"
    assert (
        conn.execute(
            "SELECT count(*) FROM api_entities WHERE version_id=?", (doc_id,)
        ).fetchone()[0]
        == 0
    )


def test_alignment_bonus_weights_evidence(conn):
    from libref.store.retriever import ALIGNMENT_BONUS, _doc_feature_bonuses

    assert ALIGNMENT_BONUS == {"symbol": 0.1, "signature": 0.05}
    library_id, version_id, _doc_id = _seed(conn)
    conn.execute(
        "INSERT INTO pages(version_id, url) VALUES(?, ?)",
        (version_id, "https://docs.example/weak"),
    )
    page_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO documents(library_id, version_id, page_id, url, content, metadata,"
        " sort_order) VALUES(?,?,?,?,?,?,0)",
        (
            library_id,
            version_id,
            page_id,
            "https://docs.example/weak",
            "weak evidence",
            '{"path":"Weak","title":"Weak"}',
        ),
    )
    weak_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO doc_api_links(library_id, version_id, document_id, api_entity_id,"
        " match_kind, confidence, evidence, tenant_id) VALUES(?,?,?,?,?,0.4,'{}','default')",
        (library_id, version_id, weak_id, 1, "name_fuzzy"),
    )
    conn.commit()

    def bonus():
        return _doc_feature_bonuses(conn, [version_id])[version_id][weak_id]

    assert bonus() == pytest.approx(0.0)
    conn.execute(
        "UPDATE doc_api_links SET match_kind='signature' WHERE document_id=?",
        (weak_id,),
    )
    assert bonus() == pytest.approx(0.05)
    conn.execute(
        "UPDATE doc_api_links SET match_kind='symbol' WHERE document_id=?",
        (weak_id,),
    )
    assert bonus() == pytest.approx(0.1)


def test_consistency_gateway_failure_is_counted(conn, monkeypatch, tmp_path):
    from libref.services.infra import vectors

    _library_id, version_id, doc_id = _seed(conn)
    conn.execute(
        "UPDATE documents SET content=content||'\ndemo.Client.close is deprecated.' WHERE id=?",
        (doc_id,),
    )

    def failing(messages):
        raise RuntimeError("offline model")

    class Gateway:
        def chat_fn(self):
            return failing

    monkeypatch.setattr(vectors, "build_gateway", lambda settings: Gateway())
    settings = make_settings(monkeypatch, tmp_path, doc_consistency_llm_enabled=True)
    counts = refresh_doc_features(conn, "demo", "1.0", settings=settings)
    assert counts["consistency_notes_failed"] >= 1
    verdicts = conn.execute(
        "SELECT facet, verdict, note FROM doc_consistency WHERE version_id=?",
        (version_id,),
    ).fetchall()
    assert ("deprecated_usage", "drift", "") in verdicts


def test_consistency_gateway_note_is_written(conn, monkeypatch, tmp_path):
    from libref.services.infra import vectors

    _library_id, version_id, doc_id = _seed(conn)
    conn.execute(
        "UPDATE documents SET content=content||'\ndemo.Client.close is deprecated.' WHERE id=?",
        (doc_id,),
    )

    class Gateway:
        def chat_fn(self):
            return lambda messages: "Retire the deprecated call."

    monkeypatch.setattr(vectors, "build_gateway", lambda settings: Gateway())
    settings = make_settings(monkeypatch, tmp_path, doc_consistency_llm_enabled=True)
    refresh_doc_features(conn, "demo", "1.0", settings=settings)
    row = conn.execute(
        "SELECT note, model, prompt_version FROM doc_consistency"
        " WHERE version_id=? AND facet='deprecated_usage'",
        (version_id,),
    ).fetchone()
    assert row[0] == "Retire the deprecated call."
    assert row[1] == "model"
    assert row[2] == "v1"


def test_job_kind_is_registered(conn, monkeypatch, tmp_path):
    from libref.services.docs.feature_jobs import (
        run_doc_feature_job_sync,
        start_doc_feature_job,
    )
    from libref.services.infra.jobs import JobManager

    _seed(conn)
    settings = make_settings(monkeypatch, tmp_path)
    mgr = JobManager(conn)
    job_id = start_doc_feature_job(mgr, "demo", "1.0", scope="structure")
    job = run_doc_feature_job_sync(mgr, settings, conn, job_id)
    assert DOC_FEATURE_KIND == "derive_doc_features"
    assert job.status == "done"
    assert job.result["features"] > 0
