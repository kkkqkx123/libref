"""Extraction, alias resolution, caching, and stdlib cross-validation."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from libref.astminer import cache as ast_cache
from libref.astminer import config as ast_config
from libref.astminer import engine as ast_engine
from libref.astminer import resolution as ast_resolution

pytestmark = pytest.mark.integration


def _parse(path: str, source: bytes):
    cfg = ast_config.PYTHON_CONFIG
    return ast_engine.extract_file(path, cfg, source)


def _resolve(decls, aliases_by_file: dict | list, raw, repo="test/repo"):
    if isinstance(aliases_by_file, list):
        path = raw[0].caller_file if raw else "a.py"
        aliases_by_file = {path: aliases_by_file}
    return ast_resolution.resolve_calls(decls, aliases_by_file, raw, repo)


def test_extract_call_shape(monkeypatch, tmp_path):
    source = b"import numpy as np\n\n\ndef load():\n    np.array([1])\n    np.zeros(3, dtype=int)\n"
    _decls, aliases, raw = _parse("a.py", source)
    assert ("np", "numpy", None) in aliases
    by_raw = {c.raw_callee: c for c in raw}
    assert "np.array" in by_raw and "np.zeros" in by_raw
    array_call = by_raw["np.array"]
    assert array_call.line_start == 5 and array_call.line_end == 5
    assert array_call.caller == "a.load"
    assert isinstance(array_call.args, list) and len(array_call.args) == 1
    assert by_raw["np.array"].args == ["[...]"]
    zeros_call = by_raw["np.zeros"]
    assert zeros_call.args[0] == "3"
    assert zeros_call.args[1].startswith("dtype=")


def test_two_pass_confidence_levels(tmp_path, monkeypatch):
    repo = "test/repo"
    decls, aliases, raw = _parse(
        "d.py", b"def helper():\n    return 1\n\n\ndef use_it():\n    helper()\n"
    )
    resolved = _resolve(decls, {"d.py": aliases}, raw, repo)
    assert resolved[0].callee == "d.helper"
    assert resolved[0].confidence == "EXTRACTED"
    decls2, aliases2, raw2 = _parse(
        "a.py", b"import numpy as np\n\n\ndef load():\n    np.array([1])\n"
    )
    resolved2 = _resolve(decls2, {"a.py": aliases2}, raw2, repo)
    assert resolved2[0].callee == "numpy.array"
    assert resolved2[0].confidence == "INFERRED"
    _d, _a, raw3 = _parse("e.py", b"def vague():\n    unknown_func(2)\n")
    resolved3 = _resolve(_d, {"e.py": _a}, raw3, repo)
    assert resolved3[0].confidence == "AMBIGUOUS"


def test_self_receiver_resolves_to_enclosing_class(monkeypatch, tmp_path):
    source = b"class A:\n    def f(self):\n        self.g()\n    def g(self):\n        pass\n"
    decls, aliases, raw = _parse("cls.py", source)
    resolved = _resolve(decls, {"cls.py": aliases}, raw, "r")
    assert resolved[0].callee == "cls.A.g"
    assert resolved[0].confidence == "EXTRACTED"


def test_cache_hit_miss_and_fingerprint_change(conn, monkeypatch, tmp_path):
    cfg = ast_config.PYTHON_CONFIG
    fp = ast_config.grammar_fingerprint(cfg)
    assert ast_cache.lookup(conn, "r", "a.py", fp, cfg.config_version) is None
    ast_cache.store(conn, "r", "a.py", "sha1", fp, cfg.config_version, [{"type": "x"}])
    conn.commit()
    hit = ast_cache.lookup(conn, "r", "a.py", fp, cfg.config_version)
    assert hit is not None and hit[0] == "sha1"
    assert ast_cache.lookup(conn, "r", "a.py", "other-fp", cfg.config_version) is None
    assert ast_cache.lookup(conn, "other-repo", "a.py", fp, cfg.config_version) is None


def test_stdlib_ast_cross_check(monkeypatch, tmp_path):
    repo_root = Path(__file__).resolve().parents[2]
    fixture = (repo_root / "tests/fixtures/mini_repo/a.py").read_bytes()
    _decls, _aliases, raw = _parse("a.py", fixture)
    tree = ast.parse(fixture.decode())
    dotted: set[str] = set()

    def dotted_of(node):
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
            name = dotted_of(node.func)
            if name:
                dotted.add(name)
    assert {c.raw_callee for c in raw} == dotted


def test_grammar_fingerprint_stable(monkeypatch, tmp_path):
    first = ast_config.grammar_fingerprint(ast_config.PYTHON_CONFIG)
    second = ast_config.grammar_fingerprint(ast_config.PYTHON_CONFIG)
    assert first == second and len(first) == hashlib.sha1(b"x").hexdigest().__len__()


def test_multi_import_all_bindings(monkeypatch, tmp_path):
    _decls, aliases, _raw = _parse("m.py", b"import os, sys\nimport numpy as np\n")
    assert ("os", "os", None) in aliases
    assert ("sys", "sys", None) in aliases
    assert ("np", "numpy", None) in aliases


def test_from_import_list_and_alias(monkeypatch, tmp_path):
    _decls, aliases, _raw = _parse("m.py", b"from m import a, b as c\n")
    by_alias = {alias: full for alias, full, _ in aliases}
    assert by_alias.get("a") == "m.a"
    assert by_alias.get("c") == "m.b"


def test_relative_import_resolves_against_module_path(monkeypatch, tmp_path):
    _decls, aliases, _raw = _parse("m.py", b"from . import x\n")
    assert aliases == []
    _decls, aliases, _raw = _parse("m.py", b"from .pkg import mod\n")
    assert aliases == [("mod", "pkg.mod", None)]
    _decls, aliases, _raw = _parse("pkg/mod.py", b"from .utils import helper\n")
    assert aliases == [("helper", "pkg.utils.helper", None)]
    _decls, aliases, _raw = _parse("pkg/__init__.py", b"from .core import start\n")
    assert aliases == [("start", "pkg.core.start", None)]


def test_arg_shapes_preserved(monkeypatch, tmp_path):
    _d, _a, raw = _parse("m.py", b"def f():\n    g(x, k=v, *args, **kw)\n")
    assert raw[0].args[0] == "x"
    assert raw[0].args[1] == "k=v"
    assert raw[0].args[2] == "*args"
    assert raw[0].args[3] == "**kw"


def test_alias_is_file_scoped(monkeypatch, tmp_path):
    decls_a, aliases_a, raw_a = _parse(
        "a.py", b"import numpy as np\ndef f():\n    np.array(1)\n"
    )
    decls_b, aliases_b, raw_b = _parse(
        "b.py", b"import networkx as np\ndef g():\n    np.graph(1)\n"
    )
    resolved = ast_resolution.resolve_calls(
        decls_a + decls_b,
        {"a.py": aliases_a, "b.py": aliases_b},
        raw_a + raw_b,
        "r",
    )
    by_raw = {(c.caller_file, c.raw_callee): c for c in resolved}
    assert by_raw[("a.py", "np.array")].callee == "numpy.array"
    assert by_raw[("b.py", "np.graph")].callee == "networkx.graph"


def test_java_receiver_and_import(monkeypatch, tmp_path):
    cfg = ast_config.JAVA_CONFIG
    decls, aliases, raw = ast_engine.extract_file(
        "A.java",
        cfg,
        b"import java.util.List;\nimport java.util.*;\n"
        b"class A { void foo(){ obj.bar(1); this.baz(); bar(2);} }\n",
    )
    assert ("List", "java.util.List", None) in aliases
    assert not [a for a in aliases if a[0] == "*"]
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["obj.bar"].caller == "A.foo"
    assert by_raw["this.baz"].caller == "A.foo"
    assert "bar" in by_raw
    resolved = ast_resolution.resolve_calls(decls, {"A.java": aliases}, raw, "r")
    by_res = {(c.caller_file, c.raw_callee): c for c in resolved}
    assert by_res[("A.java", "this.baz")].callee == "A.baz"


def test_cache_encode_decode_roundtrip(monkeypatch, tmp_path):
    decls, aliases, raw = _parse(
        "a.py", b"import numpy as np\ndef load():\n    np.array([1])\n"
    )
    records = ast_cache.encode_records(decls, aliases, raw)
    decls2, aliases2, raw2 = ast_cache.decode_records(records, "a.py")
    assert [(d.qualified_name, d.kind) for d in decls2] == [
        (d.qualified_name, d.kind) for d in decls
    ]
    assert aliases2 == aliases
    assert [(c.raw_callee, c.args) for c in raw2] == [
        (c.raw_callee, c.args) for c in raw
    ]


def test_param_types_and_return_shape(monkeypatch, tmp_path):
    decls, _, _ = _parse("m.py", b"def f(a: int, b: str) -> bool:\n    pass\n")
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["m.f"].params == ["a", "b"]
    assert by_qual["m.f"].param_types == ["int", "str"]
    assert by_qual["m.f"].return_type == "bool"


def test_var_alias_resolves_to_declared(monkeypatch, tmp_path):
    decls, aliases, raw = _parse(
        "m.py",
        b"class Foo:\n    def bar(self):\n        pass\nobj = Foo()\nobj.bar()\n",
    )
    assert ("obj", "Foo", "") in aliases
    resolved = _resolve(
        decls,
        {"m.py": aliases},
        raw,
        "r",
    )
    by_raw = {c.raw_callee: c for c in resolved}
    assert by_raw["obj.bar"].callee == "m.Foo.bar"
    assert by_raw["obj.bar"].confidence == "EXTRACTED"


def test_tie_without_signal_is_ambiguous(monkeypatch, tmp_path):
    decls_a, _, _ = _parse("a.py", b"def foo():\n    pass\n")
    decls_b, _, _ = _parse("b.py", b"def foo():\n    pass\n")
    decls = decls_a + decls_b
    declared = {d.qualified_name for d in decls}
    parent_map = {d.qualified_name: d.parent for d in decls}
    callee, conf, _kind = ast_resolution.resolve_one(
        "foo",
        "c.call",
        declared,
        set(),
        {},
        parent_map,
    )
    assert callee == "foo"
    assert conf == "AMBIGUOUS"


def test_param_shadow_blocks_global_match(monkeypatch, tmp_path):
    from libref.astminer.models import ApiDeclRecord, RawCall

    def _decl(qual, path, params=()):
        return ApiDeclRecord(
            qualified_name=qual,
            kind="function",
            file=path,
            line_start=1,
            line_end=2,
            params=list(params),
        )

    decls = [_decl("b.foo", "b.py"), _decl("b.caller", "b.py", params=["foo"])]
    raw = [
        RawCall(
            raw_callee="foo",
            caller_file="b.py",
            caller="b.caller",
            line_start=1,
            line_end=1,
        )
    ]
    resolved = ast_resolution.resolve_calls(decls, {}, raw, "r")
    assert resolved[0].confidence == "AMBIGUOUS"


def test_layout_guess_is_ambiguous_without_scope_signal(monkeypatch, tmp_path):
    from libref.astminer.models import ApiDeclRecord, RawCall

    def _decl(qual, path):
        return ApiDeclRecord(
            qualified_name=qual,
            kind="function",
            file=path,
            line_start=1,
            line_end=2,
        )

    decls = [
        _decl("pkg.mod_a.foo", "pkg/mod_a.py"),
        _decl("other.mod_b.foo", "other/mod_b.py"),
    ]
    raw = [
        RawCall(
            raw_callee="foo",
            caller_file="pkg/caller.py",
            caller="pkg.caller",
            line_start=1,
            line_end=1,
        )
    ]
    resolved = ast_resolution.resolve_calls(decls, {}, raw, "r")
    assert resolved[0].callee == "foo"
    assert resolved[0].confidence == "AMBIGUOUS"


def test_stdlib_alias_target_is_ambiguous(monkeypatch, tmp_path):
    from libref.astminer.models import ApiDeclRecord, RawCall

    decls = [
        ApiDeclRecord(
            qualified_name="m.f",
            kind="function",
            file="m.py",
            line_start=1,
            line_end=2,
        )
    ]
    raw = [
        RawCall(
            raw_callee="os.path.join",
            caller_file="m.py",
            caller="m.f",
            line_start=1,
            line_end=1,
        )
    ]
    resolved = ast_resolution.resolve_calls(
        decls, {"m.py": [("os", "os", None)]}, raw, "r"
    )
    assert resolved[0].callee == "os.path.join"
    assert resolved[0].confidence == "AMBIGUOUS"
    third = [
        RawCall(
            raw_callee="np.array",
            caller_file="m.py",
            caller="m.f",
            line_start=2,
            line_end=2,
        )
    ]
    kept = ast_resolution.resolve_calls(
        decls, {"m.py": [("np", "numpy", None)]}, third, "r"
    )
    assert kept[0].callee == "numpy.array"
    assert kept[0].confidence == "INFERRED"


def test_receiver_param_type_resolves_method(monkeypatch, tmp_path):
    from libref.astminer.models import ApiDeclRecord, RawCall

    decls = [
        ApiDeclRecord(
            qualified_name="m.Foo",
            kind="class",
            file="m.py",
            line_start=1,
            line_end=10,
        ),
        ApiDeclRecord(
            qualified_name="m.Foo.bar",
            kind="method",
            file="m.py",
            line_start=2,
            line_end=4,
        ),
        ApiDeclRecord(
            qualified_name="m.func",
            kind="function",
            file="m.py",
            line_start=5,
            line_end=8,
            params=["x"],
            param_types=["Foo"],
        ),
    ]
    raw = [
        RawCall(
            raw_callee="x.bar",
            receiver="x",
            func="bar",
            caller_file="m.py",
            caller="m.func",
            line_start=6,
            line_end=6,
        )
    ]
    resolved = ast_resolution.resolve_calls(decls, {}, raw, "r")
    assert resolved[0].callee == "m.Foo.bar"
    assert resolved[0].confidence == "EXTRACTED"


def test_reexport_closure_expands_package_alias(monkeypatch, tmp_path):
    mapping = {
        "pkg/__init__.py": {"start": "pkg.core.start"},
        "a.py": {"start": "pkg.start"},
    }
    ast_resolution._close_reexports(mapping)
    assert mapping["a.py"]["start"] == "pkg.core.start"


def test_cache_sweep_removes_stale(conn, monkeypatch, tmp_path):
    cfg = ast_config.PYTHON_CONFIG
    fp = ast_config.grammar_fingerprint(cfg)
    ast_cache.store(conn, "r", "keep.py", "s1", fp, cfg.config_version, [])
    ast_cache.store(conn, "r", "gone.py", "s2", fp, cfg.config_version, [])
    ast_cache.store(conn, "r", "keep.py", "s0", "old-fp", cfg.config_version, [])
    conn.commit()
    removed = ast_cache.sweep_repo(conn, "r", {"keep.py": (fp, cfg.config_version)})
    conn.commit()
    assert removed == 2
    assert ast_cache.lookup(conn, "r", "keep.py", fp, cfg.config_version) is not None
    assert ast_cache.lookup(conn, "r", "gone.py", fp, cfg.config_version) is None


def test_diamond_base_method_is_ambiguous(monkeypatch, tmp_path):
    from libref.astminer.models import ApiDeclRecord, RawCall

    def _decl(qual, kind="method", path="b.py", bases=()):
        return ApiDeclRecord(
            qualified_name=qual,
            kind=kind,
            file=path,
            line_start=1,
            line_end=4,
            bases=list(bases),
        )

    decls = [
        _decl("b.BaseA", kind="class"),
        _decl("b.BaseA.run"),
        _decl("b.BaseB", kind="class"),
        _decl("b.BaseB.run"),
        _decl("b.Sub", kind="class", bases=["BaseA", "BaseB"]),
        _decl("b.Sub.m"),
    ]
    raw = [
        RawCall(
            raw_callee="self.run",
            caller_file="b.py",
            caller="b.Sub.m",
            line_start=2,
            line_end=2,
        )
    ]
    resolved = ast_resolution.resolve_calls(decls, {}, raw, "r")
    assert resolved[0].confidence == "AMBIGUOUS"


def test_single_base_method_resolves_extracted(monkeypatch, tmp_path):
    from libref.astminer.models import ApiDeclRecord, RawCall

    def _decl(qual, kind="method", path="b.py", bases=()):
        return ApiDeclRecord(
            qualified_name=qual,
            kind=kind,
            file=path,
            line_start=1,
            line_end=4,
            bases=list(bases),
        )

    decls = [
        _decl("b.Base", kind="class"),
        _decl("b.Base.run"),
        _decl("b.Sub", kind="class", bases=["Base"]),
        _decl("b.Sub.m"),
    ]
    raw = [
        RawCall(
            raw_callee="self.run",
            caller_file="b.py",
            caller="b.Sub.m",
            line_start=2,
            line_end=2,
        )
    ]
    resolved = ast_resolution.resolve_calls(decls, {}, raw, "r")
    assert resolved[0].callee == "b.Base.run"
    assert resolved[0].confidence == "EXTRACTED"


def test_joint_reexport_and_chain_closure(monkeypatch, tmp_path):
    from libref.astminer.models import ApiDeclRecord, RawCall

    decls = [
        ApiDeclRecord(
            qualified_name="pkg.core.start",
            kind="function",
            file="pkg/core.py",
            line_start=1,
            line_end=2,
        )
    ]
    raw = [
        RawCall(
            raw_callee="x",
            caller_file="a.py",
            caller=None,
            line_start=1,
            line_end=1,
        )
    ]
    aliases = {
        "pkg/__init__.py": [("start", "pkg.core.start", None)],
        "a.py": [("start", "pkg.start", None), ("x", "start", "")],
    }
    resolved = ast_resolution.resolve_calls(decls, aliases, raw, "r")
    assert resolved[0].callee == "pkg.core.start"


def test_attribute_receiver_alias_resolves(monkeypatch, tmp_path):
    from libref.astminer.models import ApiDeclRecord, RawCall

    decls = [
        ApiDeclRecord(
            qualified_name="m.Foo",
            kind="class",
            file="m.py",
            line_start=1,
            line_end=4,
        ),
        ApiDeclRecord(
            qualified_name="m.Foo.bar",
            kind="method",
            file="m.py",
            line_start=2,
            line_end=3,
        ),
        ApiDeclRecord(
            qualified_name="m.C.run",
            kind="method",
            file="m.py",
            line_start=5,
            line_end=8,
        ),
    ]
    raw = [
        RawCall(
            raw_callee="self.x.bar",
            caller_file="m.py",
            caller="m.C.run",
            line_start=6,
            line_end=6,
        )
    ]
    resolved = ast_resolution.resolve_calls(
        decls, {"m.py": [("self.x", "m.Foo", "m.C")]}, raw, "r"
    )
    assert resolved[0].callee == "m.Foo.bar"
    assert resolved[0].confidence == "EXTRACTED"


def test_factory_return_type_one_hop(monkeypatch, tmp_path):
    from libref.astminer.models import ApiDeclRecord, RawCall

    decls = [
        ApiDeclRecord(
            qualified_name="m.make",
            kind="function",
            file="m.py",
            line_start=1,
            line_end=3,
            return_type="Foo",
        ),
        ApiDeclRecord(
            qualified_name="m.Foo",
            kind="class",
            file="m.py",
            line_start=4,
            line_end=8,
        ),
        ApiDeclRecord(
            qualified_name="m.Foo.bar",
            kind="method",
            file="m.py",
            line_start=5,
            line_end=6,
        ),
        ApiDeclRecord(
            qualified_name="m.f",
            kind="function",
            file="m.py",
            line_start=9,
            line_end=12,
        ),
    ]
    raw = [
        RawCall(
            raw_callee="x.bar",
            caller_file="m.py",
            caller="m.f",
            line_start=10,
            line_end=10,
        )
    ]
    resolved = ast_resolution.resolve_calls(
        decls, {"m.py": [("x", "make", "")]}, raw, "r"
    )
    assert resolved[0].callee == "m.Foo.bar"
    assert resolved[0].confidence == "EXTRACTED"
