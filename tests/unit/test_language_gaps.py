"""Closing assertions for the language-coverage gap batch.

Each test pins one gap item with a minimal shape fixture plus one golden
assertion; failures roll back the corresponding item.
"""

from __future__ import annotations

import pytest

from libref.astminer import config as ast_config
from libref.astminer import context as ast_context
from libref.astminer import engine as ast_engine
from libref.astminer import resolution as ast_resolution

pytestmark = pytest.mark.unit


def test_js_chain_inner_and_outer_calls() -> None:
    _decls, _aliases, raw = ast_engine.extract_file(
        "m.js", ast_config.JAVASCRIPT_CONFIG, b"a.b().c(1);\n"
    )
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["a.b"].receiver == "a"
    assert by_raw["a.b"].func == "b"
    assert by_raw["a.b().c"].receiver == "a.b()"
    assert by_raw["a.b().c"].func == "c"


def test_js_dynamic_import_aliases() -> None:
    _decls, aliases, raw = ast_engine.extract_file(
        "m.js",
        ast_config.JAVASCRIPT_CONFIG,
        b'const p2 = import("dyn");\nconst q = await import("./lib/dyn2");\n',
    )
    assert ("p2", "dyn") in aliases
    assert ("q", "lib.dyn2") in aliases
    assert "import" not in {c.raw_callee for c in raw}
    assert not [alias for alias, full in aliases if full in ("import", "require")]


def test_js_reexport_aliases() -> None:
    _decls, aliases, _raw = ast_engine.extract_file(
        "m.js",
        ast_config.JAVASCRIPT_CONFIG,
        b'export {a} from "m2";\nexport {b as c} from "m4";\nexport * from "m3";\n',
    )
    assert ("a", "m2.a") in aliases
    assert ("c", "m4.b") in aliases
    assert not [alias for alias, _ in aliases if alias == "*"]


def test_js_doc_comment_marks_function() -> None:
    decls, _aliases, _raw = ast_engine.extract_file(
        "m.js", ast_config.JAVASCRIPT_CONFIG, b"/** leading */\nfunction f(){}\n"
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["m.f"].has_doc is True


def test_csharp_doc_comment_marks_class_only() -> None:
    decls, _aliases, _raw = ast_engine.extract_file(
        "m.cs",
        ast_config.CSHARP_CONFIG,
        b"namespace N {\n/// <summary>Doc</summary>\nclass C { void M() {} }\n"
        b"class D { void M() {} }\n}\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["N.C"].has_doc is True
    assert by_qual["N.D"].has_doc is False


def test_groovy_doc_comment_marks_method() -> None:
    decls, _aliases, _raw = ast_engine.extract_file(
        "m.groovy", ast_config.GROOVY_CONFIG, b"class C {\n/// doc\ndef m(){}\n}\n"
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["m.C.m"].has_doc is True


def test_kotlin_primary_ctor_properties_are_fields() -> None:
    decls, _aliases, _raw = ast_engine.extract_file(
        "C.kt",
        ast_config.KOTLIN_CONFIG,
        b"package demo\nclass C(val x: Int, y: String, var z: Double) {}\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["demo.C.x"].kind == "field"
    assert by_qual["demo.C.z"].kind == "field"
    assert "demo.C.y" not in by_qual


def test_kotlin_interface_kind() -> None:
    decls, _aliases, _raw = ast_engine.extract_file(
        "F.kt",
        ast_config.KOTLIN_CONFIG,
        b"package demo\ninterface Foo { fun bar(): Int }",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["demo.Foo"].kind == "interface"
    assert by_qual["demo.Foo.bar"].kind == "method"


def test_groovy_interface_kind_and_bare_class() -> None:
    decls, _aliases, _raw = ast_engine.extract_file(
        "F.groovy",
        ast_config.GROOVY_CONFIG,
        b"package demo\ninterface Foo { def bar() }\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["demo.Foo"].kind == "interface"

    decls, _aliases, _raw = ast_engine.extract_file(
        "C.groovy", ast_config.GROOVY_CONFIG, b"class Plain { def m(){} }\n"
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["C.Plain"].kind == "class"


def test_groovy_and_kotlin_receiver_split() -> None:
    _decls, _aliases, raw = ast_engine.extract_file(
        "m.kt", ast_config.KOTLIN_CONFIG, b"fun f(){ x.toString() }\n"
    )
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["x.toString"].receiver == "x"
    assert by_raw["x.toString"].func == "toString"

    _decls, _aliases, raw = ast_engine.extract_file(
        "m.groovy", ast_config.GROOVY_CONFIG, b"def f(){ x.toString() }\n"
    )
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["x.toString"].receiver == "x"
    assert by_raw["x.toString"].func == "toString"


def test_csharp_partial_classes_merge() -> None:
    decls, _aliases, _raw = ast_engine.extract_file(
        "C.cs",
        ast_config.CSHARP_CONFIG,
        b"namespace N { public partial class C { int x; } "
        b"public partial class C { void M() {} } }",
    )
    quals = [d.qualified_name for d in decls]
    assert quals.count("N.C") == 1
    assert "N.C.M" in quals


def test_python_pyi_suffix_extracts() -> None:
    assert ast_config.config_for_suffix(".pyi") is ast_config.PYTHON_CONFIG
    assert ast_context.determine_module_path("pkg/a.pyi", "python") == "pkg.a"
    decls, _aliases, _raw = ast_engine.extract_file(
        "pkg/a.pyi", ast_config.PYTHON_CONFIG, b"def f(x: int) -> str: ...\n"
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["pkg.a.f"].kind == "function"


def test_groovy_gradle_suffix_excluded_from_source() -> None:
    assert ast_config.config_for_suffix(".gradle") is None
    assert ast_context.determine_module_path("build.gradle", "groovy") is None


def test_java_static_import_alias() -> None:
    _decls, aliases, raw = ast_engine.extract_file(
        "A.java",
        ast_config.JAVA_CONFIG,
        b"import static foo.Bar.baz;\nclass A { void m(){ baz(); } }\n",
    )
    assert ("baz", "foo.Bar.baz") in aliases
    assert {c.raw_callee for c in raw} == {"baz"}


def test_csharp_file_scoped_namespace_shape() -> None:
    decls, _aliases, _raw = ast_engine.extract_file(
        "m.cs",
        ast_config.CSHARP_CONFIG,
        b"namespace A.B;\nclass C { void M() { this.M(); } }\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["A.B"].kind == "package"
    assert by_qual["A.B.C"].kind == "class"
    assert by_qual["A.B.C.M"].kind == "method"


def test_jsx_component_yields_no_calls() -> None:
    decls, _aliases, raw = ast_engine.extract_file(
        "A.jsx",
        ast_config.JAVASCRIPT_CONFIG,
        b"function App(){ return <MyComp prop={1} />; }\n",
    )
    assert decls, "jsx file must still yield declarations"
    assert raw == []


def test_java_package_claim_over_path() -> None:
    decls, _aliases, _raw = ast_engine.extract_file(
        "other/Bar.java", ast_config.JAVA_CONFIG, b"package com.foo; class Bar {}\n"
    )
    assert {d.qualified_name for d in decls} == {"com.foo", "com.foo.Bar"}


def test_builtin_bare_calls_scoped_per_language() -> None:
    assert ast_resolution.resolve_one("len", None, {"b.len"}, set(), {}, None) == (
        "len",
        "AMBIGUOUS",
    )
    assert ast_resolution.resolve_one(
        "len", None, {"b.len"}, set(), {}, None, language="javascript"
    ) == ("b.len", "INFERRED")
    assert ast_resolution.resolve_one(
        "listOf", None, set(), set(), {}, None, language="kotlin"
    ) == ("listOf", "AMBIGUOUS")
    assert ast_resolution.resolve_one(
        "println", None, set(), set(), {}, None, language="groovy"
    ) == ("println", "AMBIGUOUS")


def test_require_destructuring_leaves_no_junk_alias() -> None:
    _decls, aliases, _raw = ast_engine.extract_file(
        "m.js",
        ast_config.JAVASCRIPT_CONFIG,
        b'const {e, f: g} = require("r2");\n',
    )
    assert ("e", "r2.e") in aliases
    assert ("g", "r2.f") in aliases
    assert all("{" not in alias and "}" not in alias for alias, _ in aliases)


def test_kotlin_secondary_constructor_entity() -> None:
    decls, _aliases, raw = ast_engine.extract_file(
        "C.kt",
        ast_config.KOTLIN_CONFIG,
        b"package demo\nclass C { constructor(y: String) { foo(y) } }\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["demo.C.constructor"].kind == "constructor"
    assert by_qual["demo.C.constructor"].params == ["y"]
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["foo"].caller == "demo.C.constructor"


def test_typescript_shapes_extract() -> None:
    decls, aliases, raw = ast_engine.extract_file(
        "m.ts",
        ast_config.TYPESCRIPT_CONFIG,
        b"interface Foo extends Bar { x: string; }\n"
        b"type X = string | number;\n"
        b"enum Color { Red, Green = 2 }\n"
        b"class C extends Base implements I { constructor(private x: number) {} m() { this.f(); } }\n"
        b"function greet(name: string): string { return name; }\n"
        b'import {a} from "m";\n',
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["m.Foo"].kind == "interface"
    assert by_qual["m.Foo"].bases == ["Bar"]
    assert by_qual["m.X"].kind == "type"
    assert by_qual["m.Color"].kind == "enum"
    assert by_qual["m.Color.Red"].kind == "enum_constant"
    assert by_qual["m.Color.Green"].kind == "enum_constant"
    assert by_qual["m.C"].bases == ["Base", "I"]
    assert by_qual["m.C.m"].kind == "method"
    assert by_qual["m.greet"].params == ["name"]
    assert ("a", "m.a") in aliases
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["this.f"].caller == "m.C.m"


def test_typescript_suffix_and_module() -> None:
    assert ast_config.config_for_suffix(".ts") is ast_config.TYPESCRIPT_CONFIG
    assert ast_config.config_for_suffix(".tsx") is ast_config.TYPESCRIPT_CONFIG
    assert ast_config.config_for_suffix(".mts") is ast_config.TYPESCRIPT_CONFIG
    assert ast_context.determine_module_path("src/a/b.ts", "typescript") == "src.a.b"
    decls, _aliases, _raw = ast_engine.extract_file(
        "a.ts", ast_config.TYPESCRIPT_CONFIG, b"function f(x: number): void {}\n"
    )
    assert {d.qualified_name for d in decls} == {"a.f"}


def test_groovy_enum_extracts() -> None:
    decls, _aliases, _raw = ast_engine.extract_file(
        "m.groovy", ast_config.GROOVY_CONFIG, b"enum Color { RED, GREEN, BLUE }\n"
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["m.Color"].kind == "enum"
    assert by_qual["m.Color.RED"].kind == "enum_constant"
    assert by_qual["m.Color.GREEN"].kind == "enum_constant"
    assert by_qual["m.Color.BLUE"].kind == "enum_constant"
    decls, _aliases, _raw = ast_engine.extract_file(
        "m.groovy",
        ast_config.GROOVY_CONFIG,
        b"package demo\nenum Color { RED, GREEN; String label }\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["demo.Color"].kind == "enum"
    assert by_qual["demo.Color.label"].kind == "field"


def test_typescript_namespace_claim_and_returns() -> None:
    decls, aliases, raw = ast_engine.extract_file(
        "m.ts",
        ast_config.TYPESCRIPT_CONFIG,
        b"namespace Demo { export class C { m(): string { return this.n(); } } }\n"
        b"function top(x: number, y?: string): string { return x; }\n"
        b"import F = require('./r');\n"
        b"declare module 'm' { export const x: number; }\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["Demo"].kind == "package"
    assert by_qual["Demo.C.m"].return_type == "string"
    assert by_qual["Demo.top"].params == ["x", "y"]
    assert by_qual["Demo.top"].param_types == ["number", "string"]
    assert by_qual["Demo.top"].return_type == "string"
    assert ("F", "r") in aliases
    assert by_qual["Demo.x"].kind == "variable"
    by_raw = {c.raw_callee: c for c in raw}
    assert by_raw["this.n"].receiver == "this"
    assert by_raw["this.n"].func == "n"


def test_typescript_ctor_param_properties_are_fields() -> None:
    decls, _aliases, _raw = ast_engine.extract_file(
        "d.ts",
        ast_config.TYPESCRIPT_CONFIG,
        b"class D { constructor(private x: number, readonly y?: string, z: boolean) {} }\n",
    )
    by_qual = {d.qualified_name: d for d in decls}
    assert by_qual["d.D.constructor.x"].kind == "field"
    assert by_qual["d.D.constructor.y"].kind == "field"
    assert "d.D.constructor.z" not in by_qual
    assert by_qual["d.D.constructor"].params == ["x", "y", "z"]
