"""Auth unit tests: loopback detection, startup rejection, bearer parsing, origin validation."""

from __future__ import annotations

import pytest

from libref.mcp_server.auth import (
    auth_required,
    check_startup,
    is_allowed_origin,
    is_loopback_host,
    parse_bearer,
    verify_key,
)


def test_loopback_hosts(monkeypatch, tmp_path) -> None:
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("example.com") is False


def test_startup_dev_mode(monkeypatch, tmp_path) -> None:
    check_startup("127.0.0.1", auth_mode="dev")
    with pytest.raises(ValueError, match="non-loopback"):
        check_startup("0.0.0.0", auth_mode="dev")


def test_startup_enforced_needs_bootstrap_or_principals(monkeypatch, tmp_path) -> None:
    with pytest.raises(ValueError, match="bootstrap"):
        check_startup("127.0.0.1", auth_mode="enforced")
    check_startup(
        "127.0.0.1",
        auth_mode="enforced",
        bootstrap_key="test-bootstrap-key",
    )
    check_startup("127.0.0.1", auth_mode="enforced", has_principals=True)
    with pytest.raises(ValueError, match="unknown auth_mode"):
        check_startup("127.0.0.1", auth_mode="unknown")


def test_auth_required_matrix(monkeypatch, tmp_path) -> None:
    assert auth_required("127.0.0.1", auth_mode="dev") is False
    assert auth_required("0.0.0.0", auth_mode="dev") is False
    assert auth_required("127.0.0.1", auth_mode="enforced") is True
    assert auth_required("0.0.0.0", auth_mode="enforced") is True


def test_parse_bearer(monkeypatch, tmp_path) -> None:
    assert parse_bearer("Bearer abc") == "abc"
    assert parse_bearer("bearer abc") == "abc"
    assert parse_bearer("Token abc") is None
    assert parse_bearer(None) is None
    assert parse_bearer("Bearer ") is None


def test_verify_key(monkeypatch, tmp_path) -> None:
    assert verify_key("raw-key", None) is False
    assert verify_key(None, "stored") is False
    assert verify_key("", "") is False
    import hashlib

    stored = hashlib.sha256(b"raw-key").hexdigest()
    assert verify_key("raw-key", stored) is True
    assert verify_key("other-key", stored) is False


def test_origin_check(monkeypatch, tmp_path) -> None:
    assert is_allowed_origin(None, host="127.0.0.1") is True
    assert is_allowed_origin("http://localhost:8000", host="127.0.0.1") is True
    assert is_allowed_origin("http://evil.example", host="127.0.0.1") is False
    assert is_allowed_origin("https://example.com/app", host="example.com") is True
