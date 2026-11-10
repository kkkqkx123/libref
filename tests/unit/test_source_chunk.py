"""Source-code grouping and direct chunking tests: attribution, docstrings, empty-chunk rejection, dual-path alignment."""

from __future__ import annotations

from libref.astminer import config as ast_config
from libref.astminer import engine as ast_engine
from libref.astminer import grouping as ast_grouping
from libref.astminer import source_chunk as ast_source_chunk
from libref.chunking import ChunkPath, default_budgets


def _extract(path: str, source: bytes):
    cfg = ast_config.PYTHON_CONFIG
    return ast_engine.extract_file(path, cfg, source)


def test_python_docstring_marks_has_doc(monkeypatch, tmp_path):
    decls, _aliases, _raw = _extract(
        "m.py",
        b'def bare():\n    pass\n\n\ndef documented():\n    """Do things."""\n    return 1\n',
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["m.bare"].has_doc is False
    assert by_qual["m.documented"].has_doc is True


def test_leading_comment_marks_has_doc(monkeypatch, tmp_path):
    decls, _aliases, _raw = _extract("m.py", b"# leading note\ndef f():\n    pass\n")
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["m.f"].has_doc is True


def test_java_javadoc_marks_has_doc(monkeypatch, tmp_path):
    cfg = ast_config.JAVA_CONFIG
    decls, _aliases, _raw = ast_engine.extract_file(
        "A.java",
        cfg,
        b"class A {\n    /** Does foo. */\n    void foo() {}\n    void bar() {}\n}\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["A.foo"].has_doc is True
    assert by_qual["A.bar"].has_doc is False


def test_class_group_absorbs_methods(monkeypatch, tmp_path):
    decls, _aliases, _raw = _extract(
        "m.py",
        b'class A:\n    """Class docs."""\n    def f(self):\n        pass\n    def g(self):\n        pass\n\n\ndef top():\n    pass\n',
    )
    groups = ast_grouping.build_groups(decls, b"", "m.py")
    assert {g.name for g in groups} == {"m.A", "m.top"}
    class_group = next(g for g in groups if g.name == "m.A")
    assert set(class_group.member_quals) == {"m.A.f", "m.A.g"}
    assert class_group.doc_members["m.A.f"] is False


def test_self_contained_member_survives_merge(monkeypatch, tmp_path):
    decls, _aliases, _raw = _extract(
        "m.py",
        b'def tiny():\n    """Documented tiny."""\n    pass\n',
    )
    groups = ast_grouping.build_groups(decls, b"def tiny():\n", "m.py")
    by_qual = {d.qualified_name: d for d in decls}
    split = ast_source_chunk.build_source_chunks(
        groups, by_qual, b'def tiny():\n    """Documented tiny."""\n    pass\n', "m.py"
    )
    assert split.chunks
    assert all(c.qual == "m.tiny" for c in split.chunks if c.qual is not None)


def test_blank_member_source_skipped_and_counted(monkeypatch, tmp_path):
    decls, _aliases, _raw = _extract("m.py", b"def f():\n    pass\n")
    groups = ast_grouping.build_groups(decls, b"", "m.py")
    by_qual = {d.qualified_name: d for d in decls}
    split = ast_source_chunk.build_source_chunks(groups, by_qual, b"", "m.py", repo="r")
    assert split.chunks == []
    assert split.empty_dropped >= 1


def test_source_chunks_share_group_across_paths(monkeypatch, tmp_path):
    source = (
        b"class Service:\n"
        b'    """Runs things."""\n'
        b"    def start(self, config):\n"
        + b"        self.configure(config)\n" * 40
        + b"    def stop(self):\n        pass\n"
    )
    decls, _aliases, _raw = _extract("svc.py", source)
    groups = ast_grouping.build_groups(decls, source, "svc.py")
    by_qual = {d.qualified_name: d for d in decls}
    split = ast_source_chunk.build_source_chunks(
        groups, by_qual, source, "svc.py", repo="r"
    )
    by_group: dict[str, set[str]] = {}
    for chunk in split.chunks:
        by_group.setdefault(chunk.group_id, set()).add(chunk.chunk_path)
    assert any(paths == {"bm25", "embedding"} for paths in by_group.values())
    kinds = {c.span_kind for c in split.chunks}
    assert kinds <= {"exact_entities", "enclosing_entity", "group_fallback"}
    assert all(c.chunk_id.startswith(f"{c.group_id}::") for c in split.chunks)


def test_source_chunk_converts_to_document_chunk(monkeypatch, tmp_path):
    decls, _aliases, _raw = _extract("m.py", b"def f():\n    pass\n")
    groups = ast_grouping.build_groups(decls, b"def f():\n    pass\n", "m.py")
    by_qual = {d.qualified_name: d for d in decls}
    split = ast_source_chunk.build_source_chunks(
        groups, by_qual, b"def f():\n    pass\n", "m.py", repo="r"
    )
    assert split.chunks
    chunk = ast_source_chunk.as_chunk(split.chunks[0], "repo://r/m.py")
    assert chunk.url == "repo://r/m.py"
    assert chunk.source_group_id == split.chunks[0].group_id
    assert chunk.chunk_id == split.chunks[0].chunk_id
    assert chunk.chunk_path in ("bm25", "embedding")


def test_large_member_splits_with_enclosing_kind(monkeypatch, tmp_path):
    source = b"def big():\n" + b"    step(1)\n" * 200
    decls, _aliases, _raw = _extract("m.py", source)
    groups = ast_grouping.build_groups(decls, source, "m.py")
    by_qual = {d.qualified_name: d for d in decls}
    budgets = default_budgets()
    split = ast_source_chunk.build_source_chunks(
        groups, by_qual, source, "m.py", budgets=budgets, repo="r"
    )
    embed = [c for c in split.chunks if c.chunk_path == ChunkPath.EMBEDDING.value]
    assert len(embed) > 1
    assert {c.span_kind for c in embed} == {"enclosing_entity"}
