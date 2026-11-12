"""Resolution scope, inheritance, factory chain, import weighting, external marking."""

from __future__ import annotations

import pytest

from libref.astminer import resolution as ast_resolution
from libref.astminer.models import ApiDeclRecord, RawCall

pytestmark = pytest.mark.integration


def _decl(qual, kind="method", path="m.py", bases=(), params=(), types=(), ret=""):
    return ApiDeclRecord(
        qualified_name=qual,
        kind=kind,
        file=path,
        line_start=1,
        line_end=4,
        params=list(params),
        param_types=list(types),
        return_type=ret,
        bases=list(bases),
    )


def _call(raw, caller, path="m.py"):
    return RawCall(
        raw_callee=raw,
        caller_file=path,
        caller=caller,
        line_start=2,
        line_end=2,
    )


def test_same_var_in_two_functions_stays_isolated():
    decls = [
        _decl("m.A", kind="class"),
        _decl("m.A.run"),
        _decl("m.B", kind="class"),
        _decl("m.B.run"),
        _decl("m.f1", kind="function"),
        _decl("m.f2", kind="function"),
    ]
    aliases = {"m.py": [("x", "A", "m.f1"), ("x", "B", "m.f2")]}
    raw = [_call("x.run", "m.f1"), _call("x.run", "m.f2")]
    resolved = ast_resolution.resolve_calls(decls, aliases, raw, "r")
    by_caller = {c.caller: c for c in resolved}
    assert by_caller["m.f1"].callee == "m.A.run"
    assert by_caller["m.f1"].confidence == "EXTRACTED"
    assert by_caller["m.f2"].callee == "m.B.run"
    assert by_caller["m.f2"].confidence == "EXTRACTED"


def test_sub_instance_inherits_base_method():
    decls = [
        _decl("m.Base", kind="class"),
        _decl("m.Base.run"),
        _decl("m.Sub", kind="class", bases=["Base"]),
        _decl("m.f", kind="function"),
    ]
    aliases = {"m.py": [("s", "Sub", "")]}
    resolved = ast_resolution.resolve_calls(
        decls, aliases, [_call("s.run", "m.f")], "r"
    )
    assert resolved[0].callee == "m.Base.run"
    assert resolved[0].confidence == "EXTRACTED"
    assert resolved[0].resolve_kind == "inherited"


def test_factory_chain_resolves_two_hops():
    decls = [
        _decl("m.outer", kind="function", ret="Mid"),
        _decl("m.Mid", kind="function", ret="Svc"),
        _decl("m.Svc", kind="class"),
        _decl("m.Svc.run"),
        _decl("m.f", kind="function"),
    ]
    aliases = {"m.py": [("x", "outer", "")]}
    resolved = ast_resolution.resolve_calls(
        decls, aliases, [_call("x.run", "m.f")], "r"
    )
    assert resolved[0].callee == "m.Svc.run"
    assert resolved[0].confidence == "EXTRACTED"
    assert resolved[0].resolve_kind == "factory"


def test_imported_candidate_wins_tie():
    decls = [
        _decl("pkg.mod_a.foo", kind="function", path="pkg/mod_a.py"),
        _decl("other.mod_b.foo", kind="function", path="other/mod_b.py"),
        _decl("pkg.caller", kind="function", path="pkg/caller.py"),
    ]
    aliases = {"pkg/caller.py": [("mod_a", "pkg.mod_a", None)]}
    raw = [_call("foo", "pkg.caller", path="pkg/caller.py")]
    resolved = ast_resolution.resolve_calls(decls, aliases, raw, "r")
    assert resolved[0].callee == "pkg.mod_a.foo"
    assert resolved[0].confidence == "INFERRED"
    assert resolved[0].resolve_kind == "import-preferred"


def test_third_party_target_is_marked_external():
    decls = [_decl("m.f", kind="function")]
    raw = [_call("np.array", "m.f")]
    resolved = ast_resolution.resolve_calls(
        decls, {"m.py": [("np", "numpy", None)]}, raw, "r"
    )
    assert resolved[0].callee == "numpy.array"
    assert resolved[0].confidence == "INFERRED"
    assert resolved[0].resolve_kind == "alias-external"
