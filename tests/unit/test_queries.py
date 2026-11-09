"""Query compilation and golden samples: four-domain queries usable, decorators and chains extractable."""

from __future__ import annotations

from tree_sitter import Query

from libref.astminer import config as ast_config
from libref.astminer import engine as ast_engine
from libref.astminer import queries as ast_queries


def test_all_queries_compile():
    for cfg in (ast_config.PYTHON_CONFIG, ast_config.JAVA_CONFIG):
        language = ast_config.load_language(cfg)
        for kind in ast_queries.KINDS:
            Query(language, ast_queries.get(cfg.name, kind))


def test_check_config_compiles_queries():
    ast_config.check_config(ast_config.PYTHON_CONFIG)
    ast_config.check_config(ast_config.JAVA_CONFIG)


def test_python_decorated_and_async_shapes():
    cfg = ast_config.PYTHON_CONFIG
    source = (
        b"import functools\n\n\n"
        b"@functools.lru_cache(maxsize=1)\n"
        b"async def fetch(url):\n"
        b"    await get(url)\n"
    )
    decls, _aliases, raw = ast_engine.extract_file("m.py", cfg, source)
    by_qual = {d.qualified_name: d for d in decls}
    assert "m.fetch" in by_qual
    assert by_qual["m.fetch"].signature.startswith("async def fetch")
    assert "url" in by_qual["m.fetch"].params
    assert {c.raw_callee for c in raw} == {"functools.lru_cache", "get"}


def test_python_chained_and_super_shapes():
    cfg = ast_config.PYTHON_CONFIG
    source = (
        b"class A(B):\n"
        b"    def f(self):\n"
        b"        self.g()\n"
        b"        a.b().c(1)\n"
        b"        super().m()\n"
    )
    decls, _aliases, raw = ast_engine.extract_file("m.py", cfg, source)
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["m.A"].bases == ["B"]
    assert by_qual["m.A.f"].kind == "method"
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["self.g"].caller == "m.A.f"
    assert by_raw["a.b().c"].caller == "m.A.f"
    assert by_raw["super().m"].caller == "m.A.f"


def test_python_wildcard_and_relative_yield_no_alias():
    cfg = ast_config.PYTHON_CONFIG
    _decls, aliases, _raw = ast_engine.extract_file(
        "m.py", cfg, b"from m import *\nfrom . import x\n"
    )
    assert aliases == []


def test_javascript_queries_compile():
    ast_config.check_config(ast_config.JAVASCRIPT_CONFIG)


def test_javascript_class_method_calls():
    cfg = ast_config.JAVASCRIPT_CONFIG
    decls, _aliases, raw = ast_engine.extract_file(
        "m.js",
        cfg,
        b"export class A extends B {\n"
        b"    m(x, y = 1, ...rest) {\n"
        b"        this.g(x); obj.h().i(1); super.s(); new a.b.C(1);\n"
        b"    }\n"
        b"}\n"
        b"export function f(a) { return g(a); }\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["m.A"].bases == ["B"]
    assert by_qual["m.A.m"].kind == "method"
    assert by_qual["m.A.m"].params == ["x", "y", "rest"]
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["this.g"].caller == "m.A.m"
    assert by_raw["obj.h().i"].caller == "m.A.m"
    assert by_raw["super.s"].caller == "m.A.m"
    assert by_raw["g"].caller == "m.f"


def test_javascript_import_require_aliases():
    cfg = ast_config.JAVASCRIPT_CONFIG
    _decls, aliases, raw = ast_engine.extract_file(
        "m.js",
        cfg,
        b'import def, {a as b, c} from "mod/x";\n'
        b'import * as ns from "lib";\n'
        b'import "side";\n'
        b'const d = require("r");\n'
        b'const {e, f: g} = require("r2");\n'
        b'export {a} from "m2";\n'
        b'const p2 = import("dyn");\n',
    )
    assert ("def", "mod/x") in aliases
    assert ("b", "mod/x.a") in aliases
    assert ("c", "mod/x.c") in aliases
    assert ("ns", "lib") in aliases
    assert ("d", "r") in aliases
    assert ("e", "r2.e") in aliases
    assert ("g", "r2.f") in aliases
    assert "require" not in {c.raw_callee for c in raw}
    assert "import" not in {c.raw_callee for c in raw}


def test_javascript_arrow_and_object_method():
    cfg = ast_config.JAVASCRIPT_CONFIG
    decls, _aliases, raw = ast_engine.extract_file(
        "m.js",
        cfg,
        b"const arrow = async (p) => helper(p);\nconst o = { k(v) { return v; } };\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["m.arrow"].kind == "function"
    assert by_qual["m.o.k"].kind == "function"
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["helper"].caller == "m.arrow"


def test_java_extends_implements_and_calls():
    cfg = ast_config.JAVA_CONFIG
    decls, aliases, raw = ast_engine.extract_file(
        "A.java",
        cfg,
        b"import java.util.List;\nimport java.util.*;\n"
        b"class A extends Base implements Runnable {\n"
        b"    void foo(){ obj.bar(1); this.baz(); bar(2); new Foo(1); }\n"
        b"}\n",
    )
    assert ("List", "java.util.List") in aliases
    assert not [a for a in aliases if a[0] == "*"]
    by_qual = {d.qualified_name: d for d in decls}
    assert "Base" in by_qual["A"].bases
    assert "Runnable" in by_qual["A"].bases
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["obj.bar"].caller == "A.foo"
    assert by_raw["this.baz"].caller == "A.foo"
    assert "Foo" in by_raw
