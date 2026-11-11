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


def test_rust_minimal_fixture_shapes(monkeypatch, tmp_path):
    data = (FIXTURES / "rust_shapes.rs").read_bytes()
    decls, aliases, raw = ast_engine.extract_file(
        "rust_shapes.rs", ast_config.RUST_CONFIG, data
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["rust_shapes.Cache"].kind == "struct"
    assert by_qual["rust_shapes.Cache.new"].kind == "method"
    assert by_qual["rust_shapes.Cache.get"].params == []
    assert "path" in by_qual["rust_shapes.run"].params
    assert ("load", "store.load") in aliases
    assert ("store_save", "store.save") in aliases
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["Cache.new"].caller == "rust_shapes.run"
    assert by_raw["load"].caller == "rust_shapes.run"
    assert by_raw["self.helper"].caller == "rust_shapes.Cache.get"


def test_go_minimal_fixture_shapes(monkeypatch, tmp_path):
    data = (FIXTURES / "go_shapes.go").read_bytes()
    decls, aliases, raw = ast_engine.extract_file(
        "go_shapes.go", ast_config.GO_CONFIG, data
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["go_shapes.Cache"].kind == "struct"
    assert by_qual["go_shapes.Cache.Get"].kind == "method"
    assert "path" in by_qual["go_shapes.Load"].params
    assert ("alias", "example.com.x.y") in aliases
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["alias.Fetch"].caller == "go_shapes.Load"
    assert by_raw["fmt.Println"].caller == "go_shapes.Load"


def test_csharp_minimal_fixture_shapes(monkeypatch, tmp_path):
    data = (FIXTURES / "csharp_shapes.cs").read_bytes()
    decls, aliases, raw = ast_engine.extract_file(
        "csharp_shapes.cs", ast_config.CSHARP_CONFIG, data
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["Store.Cache"].kind == "class"
    assert "Base" in by_qual["Store.Cache"].bases
    assert by_qual["Store.Cache.Get"].kind == "method"
    assert ("IO", "System.IO") in aliases
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["Lookup"].caller == "Store.Cache.Get"
    assert by_raw["cache.Get"].caller == "Store.Cache.Run"


def test_kotlin_minimal_fixture_shapes(monkeypatch, tmp_path):
    data = (FIXTURES / "kotlin_shapes.kt").read_bytes()
    decls, aliases, raw = ast_engine.extract_file(
        "kotlin_shapes.kt", ast_config.KOTLIN_CONFIG, data
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["demo.store.Cache"].kind == "class"
    assert by_qual["demo.store.Cache"].bases == ["Repo"]
    assert by_qual["demo.store.Cache.find"].kind == "method"
    assert by_qual["demo.store.Cache.find"].params == ["id"]
    assert by_qual["demo.store.top"].params == ["x", "y"]
    assert ("List", "java.util.List") in aliases
    assert ("U", "demo.Util") in aliases
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["U.load"].caller == "demo.store.Cache.find"
    assert by_raw["items.firstOrNull"].caller == "demo.store.Cache.find"
    assert by_raw["x.toString"].caller == "demo.store.top"


def test_groovy_minimal_fixture_shapes(monkeypatch, tmp_path):
    data = (FIXTURES / "groovy_shapes.groovy").read_bytes()
    decls, aliases, raw = ast_engine.extract_file(
        "groovy_shapes.groovy", ast_config.GROOVY_CONFIG, data
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["demo.store.Cache"].kind == "class"
    assert by_qual["demo.store.Cache"].bases == ["Base"]
    assert by_qual["demo.store.Cache.name"].kind == "field"
    assert by_qual["demo.store.Cache.run"].kind == "method"
    assert by_qual["demo.store.top"].kind == "function"
    assert by_qual["demo.store.top"].params == ["x"]
    assert ("List", "java.util.List") in aliases
    assert ("load", "demo.Util.load") in aliases
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["this.helper"].caller == "demo.store.Cache.run"
    assert by_raw["items.first"].caller == "demo.store.Cache.run"
    assert by_raw["x.toString"].caller == "demo.store.top"
    assert by_raw["Cache"].caller is None


def test_empty_source_yields_empty(monkeypatch, tmp_path):
    for cfg, path in (
        (ast_config.PYTHON_CONFIG, "empty.py"),
        (ast_config.JAVASCRIPT_CONFIG, "empty.js"),
        (ast_config.JAVA_CONFIG, "Empty.java"),
        (ast_config.RUST_CONFIG, "empty.rs"),
        (ast_config.GO_CONFIG, "empty.go"),
        (ast_config.CSHARP_CONFIG, "Empty.cs"),
        (ast_config.KOTLIN_CONFIG, "Empty.kt"),
        (ast_config.GROOVY_CONFIG, "empty.groovy"),
    ):
        decls, aliases, raw = ast_engine.extract_file(path, cfg, b"")
        assert decls == [] and aliases == [] and raw == []


def test_rust_self_and_alias_fallback_resolution(monkeypatch, tmp_path):
    data = (
        b"pub struct Cache {}\n"
        b"impl Cache {\n"
        b"    pub fn new() -> Cache { Cache {} }\n"
        b"    pub fn make() -> Cache { Self::new() }\n"
        b"}\n"
    )
    decls, aliases, raw = ast_engine.extract_file("m.rs", ast_config.RUST_CONFIG, data)
    resolved = ast_resolution.resolve_calls(decls, {"m.rs": aliases}, raw, "test/repo")
    by_raw = {(c.caller_file, c.raw_callee): c for c in resolved}
    assert by_raw[("m.rs", "Self.new")].confidence == "EXTRACTED"
    assert by_raw[("m.rs", "Self.new")].callee == "m.Cache.new"


def test_kotlin_wildcard_import_yields_no_alias(monkeypatch, tmp_path):
    _decls, aliases, _raw = ast_engine.extract_file(
        "m.kt", ast_config.KOTLIN_CONFIG, b"package a\nimport foo.*\nfun f(){ g() }\n"
    )
    assert aliases == []


def test_kotlin_delegation_bases_cover_by_and_generic(monkeypatch, tmp_path):
    data = (
        b"package demo.store\n"
        b"class C(val x: Int) : Repo by delegate, Base<String>() {\n"
        b" fun m(){}\n}\n"
    )
    decls, _aliases, _raw = ast_engine.extract_file(
        "C.kt", ast_config.KOTLIN_CONFIG, data
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert "Repo" in by_qual["demo.store.C"].bases
    assert any("Base" in b for b in by_qual["demo.store.C"].bases)


def test_groovy_qualified_extends_base(monkeypatch, tmp_path):
    data = b"package a\nclass C extends foo.Bar {\n def m(){}\n}\n"
    decls, _aliases, _raw = ast_engine.extract_file(
        "C.groovy", ast_config.GROOVY_CONFIG, data
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert any("foo.Bar" in b or b == "foo.Bar" for b in by_qual["a.C"].bases)


def test_jvm_stdlib_targets_are_ambiguous(monkeypatch, tmp_path):
    assert ast_resolution._is_stdlib_target("kotlin.collections.listOf")
    assert ast_resolution._is_stdlib_target("kotlinx.coroutines.launch")
    assert ast_resolution._is_stdlib_target("groovy.lang.Closure.call")
    assert ast_resolution._is_stdlib_target(
        "org.codehaus.groovy.runtime.DefaultGroovyMethods.plus"
    )


def test_jsx_suffix_resolves_module(monkeypatch, tmp_path):
    from libref.astminer import context as ast_context

    assert ast_context.determine_module_path("ui/App.jsx", "javascript") == "ui.App"
    assert ast_context.determine_module_path("App.jsx", "javascript") == "App"
    assert ast_config.config_for_suffix(".jsx") is ast_config.JAVASCRIPT_CONFIG
