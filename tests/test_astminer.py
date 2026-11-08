"""抽取、别名解析、缓存与标准库交叉校验。"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

from libref.astminer import cache as ast_cache
from libref.astminer import config as ast_config
from libref.astminer import engine as ast_engine
from libref.astminer import resolution as ast_resolution


def _parse(path: str, source: bytes):
    cfg = ast_config.PYTHON_CONFIG
    return ast_engine.extract_file(path, cfg, source)


def test_extract_call_shape():
    source = b"import numpy as np\n\n\ndef load():\n    np.array([1])\n    np.zeros(3, dtype=int)\n"
    decls, aliases, raw = _parse("a.py", source)
    assert ("np", "numpy") in aliases
    by_raw = {c.raw_callee: c for c in raw}
    assert "np.array" in by_raw and "np.zeros" in by_raw
    array_call = by_raw["np.array"]
    assert array_call.line_start == 5 and array_call.line_end == 5
    assert array_call.caller == "load"
    assert isinstance(array_call.args, list) and len(array_call.args) == 1


def test_two_pass_confidence_levels(tmp_path):
    repo = "test/repo"
    decls, aliases, raw = _parse(
        "d.py", b"def helper():\n    return 1\n\n\ndef use_it():\n    helper()\n"
    )
    resolved = ast_resolution.resolve_calls(decls, aliases, raw, repo)
    assert resolved[0].callee == "helper"
    assert resolved[0].confidence == "EXTRACTED"
    decls2, aliases2, raw2 = _parse(
        "a.py", b"import numpy as np\n\n\ndef load():\n    np.array([1])\n"
    )
    resolved2 = ast_resolution.resolve_calls(decls2, aliases2, raw2, repo)
    assert resolved2[0].callee == "numpy.array"
    assert resolved2[0].confidence == "INFERRED"
    _d, _a, raw3 = _parse("e.py", b"def vague():\n    unknown_func(2)\n")
    resolved3 = ast_resolution.resolve_calls(_d, _a, raw3, repo)
    assert resolved3[0].confidence == "AMBIGUOUS"


def test_self_receiver_resolves_to_enclosing_class():
    source = b"class A:\n    def f(self):\n        self.g()\n    def g(self):\n        pass\n"
    decls, aliases, raw = _parse("cls.py", source)
    resolved = ast_resolution.resolve_calls(decls, aliases, raw, "r")
    assert resolved[0].callee == "A.g"
    assert resolved[0].confidence == "EXTRACTED"


def test_cache_hit_miss_and_fingerprint_change(conn):
    cfg = ast_config.PYTHON_CONFIG
    fp = ast_config.grammar_fingerprint(cfg)
    assert ast_cache.lookup(conn, "r", "a.py", fp, cfg.config_version) is None
    ast_cache.store(conn, "r", "a.py", "sha1", fp, cfg.config_version, [{"type": "x"}])
    conn.commit()
    hit = ast_cache.lookup(conn, "r", "a.py", fp, cfg.config_version)
    assert hit is not None and hit[0] == "sha1"
    assert ast_cache.lookup(conn, "r", "a.py", "other-fp", cfg.config_version) is None
    assert ast_cache.lookup(conn, "other-repo", "a.py", fp, cfg.config_version) is None


def test_stdlib_ast_cross_check():
    fixture = Path("tests/fixtures/mini_repo/a.py").read_bytes()
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


def test_grammar_fingerprint_stable():
    first = ast_config.grammar_fingerprint(ast_config.PYTHON_CONFIG)
    second = ast_config.grammar_fingerprint(ast_config.PYTHON_CONFIG)
    assert first == second and len(first) == hashlib.sha1(b"x").hexdigest().__len__()
