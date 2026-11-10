"""Auth unit tests: loopback detection, startup rejection, token validation, origin validation."""

from __future__ import annotations

import pytest

from libref.mcp_server.auth import (
    auth_required,
    check_startup,
    is_allowed_origin,
    is_loopback_host,
    verify_bearer,
)


def test_loopback_hosts(monkeypatch, tmp_path) -> None:
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("example.com") is False


def test_startup_refuses_public_without_token(monkeypatch, tmp_path) -> None:
    check_startup("127.0.0.1", require_auth=False, token=None)
    with pytest.raises(ValueError, match="without a token"):
        check_startup("0.0.0.0", require_auth=False, token=None)
    with pytest.raises(ValueError, match="without a token"):
        check_startup("127.0.0.1", require_auth=True, token=None)
    check_startup("0.0.0.0", require_auth=False, token="tok")


def test_auth_required_matrix(monkeypatch, tmp_path) -> None:
    assert auth_required("127.0.0.1", require_auth=False, has_token=False) is False
    assert auth_required("0.0.0.0", require_auth=False, has_token=False) is True
    assert auth_required("127.0.0.1", require_auth=True, has_token=True) is True


def test_verify_bearer(monkeypatch, tmp_path) -> None:
    assert verify_bearer("Bearer abc", "abc") is True
    assert verify_bearer("bearer abc", "abc") is True
    assert verify_bearer("Bearer abc", "abd") is False
    assert verify_bearer("Bearer abc", "abcd") is False
    assert verify_bearer(None, "abc") is False
    assert verify_bearer("Bearer abc", None) is False
    assert verify_bearer("Token abc", "abc") is False


def test_origin_check(monkeypatch, tmp_path) -> None:
    assert is_allowed_origin(None, host="127.0.0.1") is True
    assert is_allowed_origin("http://localhost:8000", host="127.0.0.1") is True
    assert is_allowed_origin("http://evil.example", host="127.0.0.1") is False
    assert is_allowed_origin("https://example.com/app", host="example.com") is True
