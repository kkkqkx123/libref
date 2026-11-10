"""Real-parsing unit verification: controlled sampling plus minimal fixtures, all offline exact checks.

Blueprint: docs/tests/parsing-verification.md.
Only the assertion layer is changed; no new extraction modules.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from libref.astminer import config as ast_config
from libref.astminer import engine as ast_engine
from libref.astminer import resolution as ast_resolution

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "parsing"

pytestmark = pytest.mark.unit


def _dotted_calls(source: bytes) -> set[str]:
    tree = ast.parse(source.decode())
    out: set[str] = set()

    def dotted(node):
        parts = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = dotted(node.func)
            if name:
                out.add(name)
    return out


def _simple_calls(raw) -> set[str]:
    out: set[str] = set()
    for c in raw:
        text = c.raw_callee
        if "(" in text:
            continue
        if not text or text[0] in ('"', "'"):
            continue
        if text.startswith(('f"', "f'")):
            continue
        out.add(text)
    return out


def test_python_sampling_matches_stdlib(monkeypatch, tmp_path):
    import json

    sampling = json.loads((FIXTURES / "sampling.json").read_text(encoding="utf-8"))
    root = Path(sampling["root"])
    if not root.is_dir():
        pytest.skip("local sample root unavailable")
    sample = sampling["sample"][: sampling["max_files"]]
    assert sample
    for rel in sample:
        path = root / rel
        if not path.is_file():
            continue
        data = path.read_bytes()
        _decls, _aliases, raw = ast_engine.extract_file(
            rel, ast_config.PYTHON_CONFIG, data
        )
        assert _simple_calls(raw) == _dotted_calls(data)


def test_python_minimal_fixture_shapes(monkeypatch, tmp_path):
    data = (FIXTURES / "python_async_decorator.py").read_bytes()
    decls, aliases, raw = ast_engine.extract_file(
        "python_async_decorator.py", ast_config.PYTHON_CONFIG, data
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert "python_async_decorator.fetch" in by_qual
    assert "url" in by_qual["python_async_decorator.fetch"].params
    assert ("np", "numpy") in aliases
    by_raw = {c.raw_callee for c in raw}
    assert {"functools.lru_cache", "get", "np.array", "np.zeros", "self.step"} <= by_raw


def test_javascript_minimal_fixture_shapes(monkeypatch, tmp_path):
    data = (FIXTURES / "js_chain_import.js").read_bytes()
    decls, aliases, raw = ast_engine.extract_file(
        "js_chain_import.js", ast_config.JAVASCRIPT_CONFIG, data
    )
    assert ("b", "mod/x.a") in aliases
    assert ("ns", "lib") in aliases
    assert ("d", "r") in aliases
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["js_chain_import.Command.run"].kind == "method"
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["this.register"].caller == "js_chain_import.Command.option"
    assert by_raw["this.option"].caller == "js_chain_import.Command.run"
    assert "require" not in by_raw and "import" not in by_raw


def test_java_minimal_fixture_shapes(monkeypatch, tmp_path):
    data = (FIXTURES / "java_shapes.java").read_bytes()
    decls, aliases, raw = ast_engine.extract_file(
        "java_shapes.java", ast_config.JAVA_CONFIG, data
    )
    assert ("List", "java.util.List") in aliases
    by_qual = {d.qualified_name: d for d in decls}
    assert "Base" in by_qual["java_shapes.Mapper"].bases
    assert "Runnable" in by_qual["java_shapes.Mapper"].bases
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["obj.bar"].caller == "java_shapes.Mapper.run"
    assert by_raw["this.baz"].caller == "java_shapes.Mapper.run"


def test_alias_scope_and_confidence_fixed(monkeypatch, tmp_path):
    decls, aliases, raw = ast_engine.extract_file(
        "python_async_decorator.py",
        ast_config.PYTHON_CONFIG,
        (FIXTURES / "python_async_decorator.py").read_bytes(),
    )
    resolved = ast_resolution.resolve_calls(
        decls, {"python_async_decorator.py": aliases}, raw, "test/repo"
    )
    by_raw = {(c.caller_file, c.raw_callee): c for c in resolved}
    assert by_raw[("python_async_decorator.py", "np.array")].confidence == "INFERRED"
    assert by_raw[("python_async_decorator.py", "self.step")].confidence == "EXTRACTED"


def test_empty_source_yields_empty(monkeypatch, tmp_path):
    for cfg, path in (
        (ast_config.PYTHON_CONFIG, "empty.py"),
        (ast_config.JAVASCRIPT_CONFIG, "empty.js"),
        (ast_config.JAVA_CONFIG, "Empty.java"),
    ):
        decls, aliases, raw = ast_engine.extract_file(path, cfg, b"")
        assert decls == [] and aliases == [] and raw == []
