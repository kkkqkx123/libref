"""Query compilation and golden samples: four-domain queries usable, decorators and chains extractable."""

from __future__ import annotations

from tree_sitter import Query

from libref.astminer import config as ast_config
from libref.astminer import engine as ast_engine
from libref.astminer import queries as ast_queries


def test_all_queries_compile(monkeypatch, tmp_path):
    for cfg in ast_config.LANGUAGE_CONFIGS.values():
        language = ast_config.load_language(cfg)
        for kind in ast_queries.KINDS:
            Query(language, ast_queries.get(cfg.name, kind))


def test_check_config_compiles_queries(monkeypatch, tmp_path):
    for cfg in ast_config.LANGUAGE_CONFIGS.values():
        ast_config.check_config(cfg)


def test_python_decorated_and_async_shapes(monkeypatch, tmp_path):
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
    signature = by_qual["m.fetch"].signature
    assert "fetch" in signature and "url" in signature
    assert "@" not in signature and "await get" not in signature
    assert "url" in by_qual["m.fetch"].params
    assert {c.raw_callee for c in raw} == {"functools.lru_cache", "get"}


def test_python_chained_and_super_shapes(monkeypatch, tmp_path):
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


def test_python_wildcard_and_relative_yield_no_alias(monkeypatch, tmp_path):
    cfg = ast_config.PYTHON_CONFIG
    _decls, aliases, _raw = ast_engine.extract_file(
        "m.py", cfg, b"from m import *\nfrom . import x\n"
    )
    assert aliases == []


def test_javascript_queries_compile(monkeypatch, tmp_path):
    ast_config.check_config(ast_config.JAVASCRIPT_CONFIG)


def test_new_language_queries_compile(monkeypatch, tmp_path):
    ast_config.check_config(ast_config.RUST_CONFIG)
    ast_config.check_config(ast_config.GO_CONFIG)
    ast_config.check_config(ast_config.CSHARP_CONFIG)


def test_javascript_class_method_calls(monkeypatch, tmp_path):
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


def test_javascript_import_require_aliases(monkeypatch, tmp_path):
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
    assert ("def", "mod/x", None) in aliases
    assert ("b", "mod/x.a", None) in aliases
    assert ("c", "mod/x.c", None) in aliases
    assert ("ns", "lib", None) in aliases
    assert ("d", "r", None) in aliases
    assert ("e", "r2.e", None) in aliases
    assert ("g", "r2.f", None) in aliases
    assert "require" not in {c.raw_callee for c in raw}
    assert "import" not in {c.raw_callee for c in raw}


def test_javascript_arrow_and_object_method(monkeypatch, tmp_path):
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


def test_java_extends_implements_and_calls(monkeypatch, tmp_path):
    cfg = ast_config.JAVA_CONFIG
    decls, aliases, raw = ast_engine.extract_file(
        "A.java",
        cfg,
        b"import java.util.List;\nimport java.util.*;\n"
        b"class A extends Base implements Runnable {\n"
        b"    void foo(){ obj.bar(1); this.baz(); bar(2); new Foo(1); }\n"
        b"}\n",
    )
    assert ("List", "java.util.List", None) in aliases
    assert not [a for a in aliases if a[0] == "*"]
    by_qual = {d.qualified_name: d for d in decls}
    assert "Base" in by_qual["A"].bases
    assert "Runnable" in by_qual["A"].bases
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["obj.bar"].caller == "A.foo"
    assert by_raw["this.baz"].caller == "A.foo"
    assert "Foo" in by_raw


def test_rust_impl_methods_and_use_aliases(monkeypatch, tmp_path):
    cfg = ast_config.RUST_CONFIG
    decls, aliases, raw = ast_engine.extract_file(
        "src/main.rs",
        cfg,
        b"use std::collections::HashMap;\n"
        b"use crate::store::{load, save as store_save};\n"
        b"pub struct Cache { items: u32 }\n"
        b"impl Cache {\n"
        b"    pub fn new() -> Cache { Cache { items: 0 } }\n"
        b"    pub fn get(&self) -> u32 { self.helper() }\n"
        b"}\n"
        b'fn main() { let c = Cache::new(); c.get(); load("f"); }\n',
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["Cache"].kind == "struct"
    assert by_qual["Cache.new"].kind == "method"
    assert by_qual["Cache.get"].kind == "method"
    assert ("load", "store.load", None) in aliases
    assert ("store_save", "store.save", None) in aliases
    assert not [a for a in aliases if a[0] == "HashMap"]
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["Cache.new"].caller == "main"
    assert by_raw["load"].caller == "main"
    assert by_raw["self.helper"].caller == "Cache.get"


def test_go_receiver_and_grouped_imports(monkeypatch, tmp_path):
    cfg = ast_config.GO_CONFIG
    decls, aliases, raw = ast_engine.extract_file(
        "store/cache.go",
        cfg,
        b"package store\n"
        b"import (\n"
        b'\t"fmt"\n'
        b'\talias "example.com/x/y"\n'
        b")\n"
        b"type Cache struct { items int }\n"
        b"func (c *Cache) Get() int { return c.items }\n"
        b"func Load(path string) int {\n"
        b"\tfmt.Println(path)\n"
        b"\treturn alias.Fetch(path)\n"
        b"}\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["store.cache.Cache"].kind == "struct"
    assert by_qual["store.cache.Cache.Get"].kind == "method"
    assert by_qual["store.cache.Load"].params == ["path"]
    assert ("alias", "example.com.x.y", None) in aliases
    assert not [a for a in aliases if a[0] == "fmt"]
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["alias.Fetch"].caller == "store.cache.Load"
    assert by_raw["fmt.Println"].caller == "store.cache.Load"


def test_csharp_namespace_using_and_calls(monkeypatch, tmp_path):
    cfg = ast_config.CSHARP_CONFIG
    decls, aliases, raw = ast_engine.extract_file(
        "Store/Cache.cs",
        cfg,
        b"using System;\n"
        b"using IO = System.IO;\n"
        b"namespace Store {\n"
        b"class Cache : Base {\n"
        b"    public int Get(string key) { return this.Lookup(key); }\n"
        b"    public void Run(string path) {\n"
        b"        var cache = new Cache();\n"
        b"        cache.Get(path);\n"
        b"        IO.File.Delete(path);\n"
        b"    }\n"
        b"}\n"
        b"}\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["Store"].kind == "package"
    assert by_qual["Store.Cache"].bases == ["Base"]
    assert by_qual["Store.Cache.Get"].kind == "method"
    assert by_qual["Store.Cache.Get"].params == ["key"]
    assert ("IO", "System.IO", None) in aliases
    assert ("System", "System", None) in aliases
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["Lookup"].caller == "Store.Cache.Get"
    assert by_raw["cache.Get"].caller == "Store.Cache.Run"
    assert by_raw["IO.File.Delete"].caller == "Store.Cache.Run"
    assert by_raw["Cache"].caller == "Store.Cache.Run"


def test_rust_macro_definition_and_call(monkeypatch, tmp_path):
    cfg = ast_config.RUST_CONFIG
    decls, _aliases, raw = ast_engine.extract_file(
        "m.rs",
        cfg,
        b"macro_rules! myvec { ($($x:expr),*) => { vec![$($x),*] }; }\n"
        b"fn f() { myvec![1, 2]; crate::m::other!(3); }\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["m.myvec"].kind == "macro"
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["myvec"].caller == "m.f"
    assert by_raw["m.other"].caller == "m.f"


def test_rust_generic_calls_strip_type_arguments(monkeypatch, tmp_path):
    cfg = ast_config.RUST_CONFIG
    _decls, _aliases, raw = ast_engine.extract_file(
        "m.rs",
        cfg,
        b"fn f() { let a = foo::<i32>(1); obj.bar::<u8>(2); }\n",
    )
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["foo"].caller == "m.f"
    assert by_raw["obj.bar"].caller == "m.f"


def test_rust_use_path_keywords_resolve_against_module(monkeypatch, tmp_path):
    cfg = ast_config.RUST_CONFIG
    _decls, aliases, _raw = ast_engine.extract_file(
        "a/b.rs",
        cfg,
        b"use super::sibling::Thing;\n"
        b"use self::inner::Item;\n"
        b"use crate::top::Top;\n"
        b"use plain::P;\n",
    )
    assert ("Thing", "a.sibling.Thing", None) in aliases
    assert ("Item", "a.b.inner.Item", None) in aliases
    assert ("Top", "top.Top", None) in aliases
    assert ("P", "plain.P", None) in aliases


def test_csharp_generic_calls_use_base_name(monkeypatch, tmp_path):
    cfg = ast_config.CSHARP_CONFIG
    _decls, _aliases, raw = ast_engine.extract_file(
        "m.cs",
        cfg,
        b"class A { void M() {\n"
        b"    this.G<int>(1); Helper.H<string>(x); G2<int, string>(y);\n"
        b"} }\n",
    )
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["G"].caller == "m.A.M"
    assert by_raw["Helper.H"].caller == "m.A.M"
    assert by_raw["G2"].caller == "m.A.M"


def test_csharp_nested_and_file_scoped_namespaces(monkeypatch, tmp_path):
    cfg = ast_config.CSHARP_CONFIG
    decls, _aliases, _raw = ast_engine.extract_file(
        "m.cs",
        cfg,
        b"namespace A { namespace B { class C { void M() {} } } }\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["A.B.C"].kind == "class"
    assert by_qual["A.B.C.M"].kind == "method"
    assert "B" in by_qual["A.B"].bases or by_qual["A.B"].kind == "package"

    decls, _aliases, _raw = ast_engine.extract_file(
        "m.cs",
        cfg,
        b"namespace A.B;\nclass C { void M() { this.M(); } }\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["A.B.C.M"].kind == "method"
    assert by_qual["A.B"].kind == "package"


def test_go_import_prefix_normalization(monkeypatch, tmp_path):
    from libref.astminer import rules as ast_rules

    assert (
        ast_rules.normalize_go_import("example.com.demo.pkg", "example.com.demo")
        == "pkg"
    )
    assert (
        ast_rules.normalize_go_import("github.com.x.y", "example.com.demo")
        == "github.com.x.y"
    )
    assert (
        ast_rules.normalize_go_import("example.com.demo", "example.com.demo")
        == "example.com.demo"
    )
    assert ast_rules.normalize_go_import("fmt", "") == "fmt"


def test_capture_contract_accepts_all_languages(monkeypatch, tmp_path):
    for cfg in ast_config.LANGUAGE_CONFIGS.values():
        ast_queries.validate_captures(cfg.name)


def test_capture_contract_rejects_unknown_name(monkeypatch, tmp_path):
    import pytest

    with pytest.raises(ValueError):
        ast_queries.validate_names(["entity.function.name", "entity.funciton.name"])
