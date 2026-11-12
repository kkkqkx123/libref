"""Symbol and relation usage gap: persistence plus downstream consumption."""

from __future__ import annotations

import pytest

from libref.mining.feature_tree import build_feature_tree
from libref.mining.induction import InductionInput, build_label_prompt
from libref.services.code.code_ingest import index_repo
from libref.services.code.mining_read import MiningReadService
from libref.services.code.mining_refresh import mine_patterns

pytestmark = pytest.mark.integration


def _write_repo(root) -> None:
    pkg = root / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(
        '"""Core service."""\n\n\n'
        "class Service:\n"
        '    """Service docs."""\n\n'
        "    def create(self, name: str) -> str:\n"
        '        """Create docs."""\n'
        "        return name\n\n"
        "    def run(self, name: str) -> str:\n"
        "        return self.create(name)\n",
        encoding="utf-8",
    )
    (pkg / "factory.py").write_text(
        "from pkg.core import Service\n\n\n"
        "def make_service() -> Service:\n"
        '    """Make docs."""\n'
        "    return Service()\n",
        encoding="utf-8",
    )
    (pkg / "app.py").write_text(
        "import pkg.core as core\n"
        "from pkg.factory import make_service\n\n\n"
        "def main() -> str:\n"
        "    first = make_service().run(\"b\")\n"
        "    svc = core.Service()\n"
        "    second = svc.create(\"a\")\n"
        "    return first + second\n",
        encoding="utf-8",
    )


def _index_symbol_repo(conn, tmp_path):
    root = tmp_path / "symrepo"
    _write_repo(root)
    index_repo(conn, "demo", "0.1", str(root), repos_dir=tmp_path / "repos")
    return root


def test_symbol_detail_persisted(conn, tmp_path):
    _index_symbol_repo(conn, tmp_path)
    row = conn.execute(
        "SELECT file, line_start, line_end, params, param_types,"
        " return_type, has_doc, parent FROM api_entities"
        " WHERE qualified_name=?",
        ("pkg.core.Service.create",),
    ).fetchone()
    assert row is not None
    file, ls, le, params, ptypes, ret, has_doc, parent = row
    assert str(file).endswith("pkg/core.py")
    assert int(ls) >= 1 and int(le) >= int(ls)
    assert "name" in str(params)
    assert "str" in str(ptypes)
    assert str(ret) == "str"
    assert int(has_doc) == 1
    assert parent == "pkg.core.Service"


def test_call_resolve_and_caller_link(conn, tmp_path):
    _index_symbol_repo(conn, tmp_path)
    rows = conn.execute(
        "SELECT callee_name, resolve_kind, caller, caller_entity_id,"
        " receiver, func FROM call_instances"
    ).fetchall()
    assert rows
    allowed = {
        "alias-direct",
        "alias-suffix",
        "stdlib",
        "factory",
        "self-direct",
        "self-inherited",
        "receiver-type",
        "same-file",
        "suffix",
        "scope",
        "builtin",
        "param-shadowed",
        "unresolved",
    }
    for callee, kind, _caller, _link, _recv, _func in rows:
        assert str(kind) in allowed
    in_main = [r for r in rows if r[2] == "pkg.app.main"]
    assert in_main
    linked = [r for r in in_main if r[3] is not None]
    assert linked
    target = conn.execute(
        "SELECT id FROM api_entities WHERE qualified_name=?",
        ("pkg.app.main",),
    ).fetchone()
    assert target is not None
    assert any(int(r[3]) == int(target[0]) for r in linked)


def test_import_edges_queryable(conn, tmp_path):
    _index_symbol_repo(conn, tmp_path)
    svc = MiningReadService(conn)
    by_file = svc.imports_of_file("pkg/app.py", "demo", "0.1")
    assert by_file
    assert any(r["target"] == "pkg.core" for r in by_file)
    assert any(r["target"] == "pkg.factory.make_service" for r in by_file)
    importers = svc.importers_of("pkg.core", "demo", "0.1")
    assert any(r["importer_file"] == "pkg/app.py" for r in importers)


def test_feature_tree_prefers_true_ownership():
    calls = [("a.b.c", "s1")]
    nodes = {
        n["path"]: n
        for n in build_feature_tree(
            calls, {"a.b.c": "function", "a": "package"}, {"a.b.c": "a"}
        )
    }
    assert "a.b" not in nodes
    assert nodes["a.b.c"]["parent"] == "a"
    assert nodes["a"]["calls"] == 1


def test_evidence_carries_signatures(conn, tmp_path):
    _index_symbol_repo(conn, tmp_path)
    mine_patterns(conn, "demo", "0.1", minsup=1, minlen=1)
    svc = MiningReadService(conn)
    patterns = svc.list_patterns("demo", "0.1", limit=10)
    assert patterns
    target = None
    for entry in patterns:
        names = list(entry.get("pattern") or [])
        if "pkg.core.Service.run" in names or "pkg.core.Service.create" in names:
            target = names
            break
    assert target is not None
    groups = svc.pattern_evidence(target, "demo", "0.1", limit=5)
    assert groups
    assert any(
        str(c.get("callee_signature") or "").strip()
        for g in groups
        for c in (g.get("calls") or [])
    )
    assert any("<call" in str(g.get("rendered") or "") for g in groups)


def test_induction_prompt_carries_signatures():
    item = InductionInput(
        pattern=["pkg.core.Service.run"],
        support=2,
        arg_shapes=[{"api": "pkg.core.Service.run", "args": ["x"], "count": 2}],
        siblings=[],
        scope="demo@0.1",
        signatures={"pkg.core.Service.run": "pkg.core.Service.run(name: str) -> str"},
    )
    content = build_label_prompt(item)[1]["content"]
    assert "pkg.core.Service.run(name: str) -> str" in content
    assert "def " not in content
    plain = InductionInput(pattern=["pkg.core.Service.run"], support=1)
    assert "Signatures:" not in build_label_prompt(plain)[1]["content"]
