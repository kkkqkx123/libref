"""Differential checks for C/C++ parsing, macro facts, stdlib and span mapping."""

from __future__ import annotations

import pytest

from libref.astminer import code_blocks as blocks
from libref.astminer import config as ast_config
from libref.astminer import engine as ast_engine
from libref.astminer import resolution as ast_resolution
from libref.astminer import stdlib as ast_stdlib
from libref.astminer.c_shadow import shadow_for_parse
from libref.astminer.models import ApiDeclRecord, CodeSnippet, RawCall

pytestmark = pytest.mark.unit


def test_header_suffix_uses_cpp_and_c_stays_c():
    assert ast_config.config_for_suffix(".h") is ast_config.CPP_CONFIG
    assert ast_config.config_for_suffix(".c") is ast_config.C_CONFIG


def test_shadow_blank_pairs_and_preserves_original_spans():
    source = (
        b"#ifndef _foo_h_\n#define _foo_h_\n"
        b'  #if defined(__cplusplus)\n  extern "C" {\n#endif\n'
        b"int foo(void);\n"
        b"#ifdef __cplusplus\n  }\n#endif\n#endif\n"
    )
    shadow = shadow_for_parse(source, "cpp")
    assert len(shadow) == len(source)
    assert b'extern "C"' not in shadow
    assert b"#if defined(__cplusplus)" in shadow
    blanked = [
        line
        for line in shadow.split(b"\n")
        if b"extern" in line or line.strip() == b"}"
    ]
    assert blanked == []
    decls, _aliases, _raw = ast_engine.extract_file(
        "m.h", ast_config.CPP_CONFIG, source
    )
    names = [d.qualified_name for d in decls]
    assert "foo" in names
    foo = next(d for d in decls if d.qualified_name == "foo")
    assert source[foo.start_byte : foo.end_byte].startswith(b"int foo")


def test_shadow_rejects_unbalanced_and_other_languages():
    source = b'#ifdef __cplusplus\nextern "C" {\n#endif\nint a;\n#endif\n'
    assert shadow_for_parse(source, "c") == source
    assert shadow_for_parse(source, "python") == source
    crlf = (
        b'#ifdef __cplusplus\r\nextern "C" {\r\n#endif\r\n'
        b"int a;\r\n#ifdef __cplusplus\r\n}\r\n#endif\r\n"
    )
    out = shadow_for_parse(crlf, "cpp")
    assert len(out) == len(crlf)
    assert b'extern "C"' not in out
    assert all(
        line.strip() == b"" for line in out.split(b"\n")[1:2] + out.split(b"\n")[5:6]
    )


def test_c_queries_capture_macros_conditions_and_call_shapes():
    source = (
        b"#define SQUARE(x) ((x) * (x))\n"
        b"#define WRAP(x) target_call(x)\n"
        b"#ifdef FEATURE_A\n#if defined(OTHER)\n"
        b"int helper(int a) { return a; }\n#endif\n#endif\n"
        b"void use(int a) {\n"
        b"  int (*fp)(int) = helper;\n"
        b"  (*fp)(a);\n"
        b"  SQUARE(a);\n"
        b"  WRAP(a);\n"
        b"}\n"
    )
    decls, _aliases, raw = ast_engine.extract_file("m.c", ast_config.C_CONFIG, source)
    by_name = {d.qualified_name: d for d in decls}
    assert by_name["SQUARE"].kind == "macro"
    assert by_name["SQUARE"].params == ["x"]
    assert by_name["helper"].preproc_conditions == ["FEATURE_A", "OTHER"]
    calls = {(c.raw_callee, c.relation_kind) for c in raw}
    assert ("target_call", "macro") in calls
    assert ("SQUARE", "macro") in calls
    assert ("fp", "pointer") in calls
    assert ("helper", "funcref") in calls
    assert sum(1 for c in raw if c.raw_callee == "SQUARE") == 1


def test_cpp_class_members_and_constructor_are_extracted():
    source = (
        b'#ifdef __cplusplus\nextern "C" {\n#endif\n'
        b"class Cache {\npublic:\n  Cache();\n  ~Cache();\n  int Get(int id);\n};\n"
        b"int Cache::Get(int id) {\n  this->Reset();\n  return id;\n}\n"
        b"#ifdef __cplusplus\n}\n#endif\n"
    )
    decls, _aliases, _raw = ast_engine.extract_file(
        "m.h", ast_config.CPP_CONFIG, source
    )
    kinds = {(d.qualified_name, d.kind) for d in decls}
    assert ("Cache", "class") in kinds
    assert ("Cache.~Cache", "constructor") in kinds
    assert ("Cache.Get", "method") in kinds
    assert source[decls[0].start_byte : decls[0].end_byte].startswith(b"class Cache")


def test_cpp_receivers_are_kept_for_qualified_and_member_calls():
    source = (
        b"class Foo { public: void method(int x); };\n"
        b"void method(int x);\n"
        b"void use(Foo f) { Foo::method(1); f.method(2); }\n"
    )
    decls, aliases, raw = ast_engine.extract_file("m.h", ast_config.CPP_CONFIG, source)
    receivers = {c.raw_callee: c.receiver for c in raw}
    assert receivers["Foo.method"] == "Foo"
    assert receivers["f.method"] == "f"
    resolved = ast_resolution.resolve_calls(
        decls, {"m.h": aliases}, raw, "repo", language="cpp"
    )
    bound = {c.raw_callee: c.callee for c in resolved}
    assert bound["Foo.method"] == "Foo.method"
    assert bound["f.method"] != "method"


def test_rust_macro_body_and_argument_calls_are_low_confidence():
    source = (
        b"fn helper(x: i32) -> i32 { x }\n"
        b'fn run() { println!("{}", helper(1)); }\n'
        b"macro_rules! wrap { ($e:expr) => { helper($e); }; }\n"
    )
    decls, aliases, raw = ast_engine.extract_file(
        "m.rs", ast_config.RUST_CONFIG, source
    )
    calls = [c for c in raw if c.raw_callee == "helper"]
    assert len(calls) == 2
    assert all(c.relation_kind == "macro" and c.macro_generated for c in calls)
    resolved = ast_resolution.resolve_calls(
        decls, {"m.rs": aliases}, raw, "repo", language="rust"
    )
    macro_calls = [c for c in resolved if c.relation_kind == "macro"]
    assert macro_calls
    assert all(c.confidence == "INFERRED" for c in macro_calls)


def test_stdlib_tables_classify_before_target_hits():
    assert ast_stdlib.is_stdlib("printf", "c")
    assert ast_stdlib.is_stdlib("malloc", "cpp")
    assert ast_stdlib.is_stdlib("std::vector", "cpp")
    assert ast_stdlib.is_stdlib("std::collections::HashMap", "rust")
    assert not ast_stdlib.is_stdlib("rte_eth_rx_burst", "c")
    decls = [
        ApiDeclRecord(
            qualified_name="printf",
            kind="function",
            file="m.c",
            line_start=1,
            line_end=1,
        )
    ]
    call = RawCall(raw_callee="printf", caller_file="m.c", line_start=1, line_end=1)
    resolved = ast_resolution.resolve_calls(decls, {}, [call], "repo", language="c")
    assert resolved[0].callee == "printf"
    assert resolved[0].resolve_kind != "stdlib"


def test_stdlib_can_be_left_unclassified():
    call = RawCall(raw_callee="printf", caller_file="m.c", line_start=1, line_end=1)
    resolved = ast_resolution.resolve_calls(
        [], {}, [call], "repo", language="c", stdlib_filter_enabled=False
    )
    assert resolved[0].resolve_kind != "stdlib"


def test_non_self_receiver_does_not_hijack_local_function():
    decls = [
        ApiDeclRecord(
            qualified_name="m.method",
            kind="function",
            file="m.py",
            line_start=1,
            line_end=2,
        )
    ]
    call = RawCall(
        raw_callee="obj.method",
        receiver="obj",
        func="method",
        caller_file="m.py",
        caller="m.f",
        line_start=1,
        line_end=1,
        argument_count=1,
    )
    resolved = ast_resolution.resolve_calls(
        decls, {}, [call], "repo", language="python"
    )
    assert resolved[0].callee == "obj.method"
    assert resolved[0].confidence == "AMBIGUOUS"


def test_code_snippet_origin_positions_map_to_host():
    document = "# Quickstart\n\n```python\ndemo.run()\n```\n"
    block = blocks.fenced_blocks(document, sample_prefix="doc", url="https://x/p")[0]
    assert block.origin_location == "https://x/p"
    assert document[block.origin_start_byte : block.origin_end_byte] == "demo.run()"
    assert block.origin_start_line == 4

    rust = b"fn x() {\n/// ```rust\n/// libref::run();\n/// ```\n}\n"
    doc = blocks.rust_doc_blocks(rust, path="src/lib.rs")[0]
    assert doc.origin_location == "src/lib.rs"
    assert doc.origin_start_line == 3
    assert doc.origin_start_byte > 0

    snippet = CodeSnippet(sample_id="s", language="python", code="demo.run()")
    assert snippet.origin_location == ""


def test_probe_reports_role_candidates_without_changing_roles(tmp_path):
    from libref.services.code.probe import probe_repo

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text(
        "def test_demo():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "plain.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    probe = probe_repo(str(tmp_path))
    by_file = {c.file: c for c in probe.role_candidates}
    candidate = by_file["tests/test_demo.py"]
    assert candidate.role == "test"
    assert {"path", "annotation", "name"} <= set(candidate.sources)
    assert "plain.py" not in by_file
