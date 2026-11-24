"""Server-local identity commands reuse service rules and write audit rows."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3

import pytest

from libref.cli.parsers import build_parser
from libref.db import apply_migrations, open_db

pytestmark = pytest.mark.integration


def _open(tmp_path) -> sqlite3.Connection:
    conn = open_db(tmp_path / "keys.db", check_same_thread=False)
    apply_migrations(conn)
    return conn


def _run(argv: list[str]) -> tuple[int, str]:
    args = build_parser().parse_args(argv)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = int(args.func(args))
    return code, out.getvalue().strip()


def _env(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LIBREF_DB_PATH", str(tmp_path / "keys.db"))
    monkeypatch.setenv("LIBREF_AUTH_MODE", "enforced")


def test_issue_list_revoke_and_audit(tmp_path, monkeypatch) -> None:
    _env(monkeypatch, tmp_path)
    code, payload = _run(
        [
            "key",
            "--json",
            "issue",
            "--name",
            "operator",
            "--role",
            "developer",
            "--scope",
            "read",
        ]
    )
    assert code == 0
    issued = json.loads(payload)
    raw = issued["key"]
    assert len(raw) > 12

    conn = _open(tmp_path)
    try:
        stored = conn.execute(
            "SELECT key_hash, key_prefix FROM api_keys WHERE id=?", (issued["id"],)
        ).fetchone()
        assert stored is not None
        assert stored[0] != raw
        assert stored[1] == raw[:12]

        code, payload = _run(["key", "--json", "list"])
        assert code == 0
        rows = json.loads(payload)
        assert rows[0]["key_prefix"] == raw[:12]
        assert "key" not in rows[0]

        assert _run(["key", "revoke", str(issued["id"])]) == (0, "ok")
        revoked = conn.execute(
            "SELECT revoked_at FROM api_keys WHERE id=?", (issued["id"],)
        ).fetchone()
        assert revoked is not None and revoked[0] is not None

        actions = conn.execute(
            "SELECT action, principal_id, result FROM audit_log"
            " WHERE action LIKE 'cli.%' ORDER BY id"
        ).fetchall()
        assert ("cli.keys.issue", None, "ok") in actions
        assert ("cli.keys.list", None, "ok") in actions
        assert ("cli.keys.revoke", None, "ok") in actions
    finally:
        conn.close()


def test_attach_and_revoke_all(tmp_path, monkeypatch) -> None:
    _env(monkeypatch, tmp_path)
    _, payload = _run(["key", "--json", "issue", "--name", "svc", "--role", "external"])
    principal_id = json.loads(payload)["principal_id"]
    _, attached = _run(
        ["key", "--json", "attach", str(principal_id), "--scope", "read"]
    )
    second = json.loads(attached)
    assert second["principal_id"] == principal_id

    conn = _open(tmp_path)
    try:
        assert _run(["key", "revoke-all", str(principal_id)]) == (0, "revoked: 2")
        active = conn.execute(
            "SELECT COUNT(*) FROM api_keys WHERE principal_id=? AND revoked_at IS NULL",
            (principal_id,),
        ).fetchone()
        assert active is not None and active[0] == 0
        assert ("cli.keys.revoke_all", None, "ok") in conn.execute(
            "SELECT action, principal_id, result FROM audit_log"
        ).fetchall()
    finally:
        conn.close()


def test_service_rules_still_reject_overreach(tmp_path, monkeypatch) -> None:
    _env(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="cannot grant scopes"):
        _run(
            [
                "key",
                "--json",
                "issue",
                "--name",
                "bad",
                "--role",
                "developer",
                "--scope",
                "admin:keys",
            ]
        )
    with pytest.raises(ValueError, match="separation of duties"):
        _run(
            [
                "key",
                "--json",
                "issue",
                "--name",
                "bad",
                "--role",
                "developer",
                "--role",
                "kb_admin",
            ]
        )
