"""Request auth unit tests: startup, bearer parsing, origin matching, centralized gate."""

from __future__ import annotations

import pytest

from libref.services.infra.auth import (
    AuthDenied,
    authenticate_request,
    check_startup,
    dev_principal,
    is_allowed_origin,
    is_loopback_host,
    parse_bearer,
    validate_origin_config,
    verify_key,
)
from tests.conftest import make_settings


def test_loopback_hosts() -> None:
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("example.com") is False


def test_startup_dev_mode() -> None:
    check_startup("127.0.0.1", auth_mode="dev")
    with pytest.raises(ValueError, match="non-loopback"):
        check_startup("0.0.0.0", auth_mode="dev")


def test_startup_enforced_needs_bootstrap_or_principals() -> None:
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


def test_parse_bearer() -> None:
    assert parse_bearer("Bearer abc") == "abc"
    assert parse_bearer("bearer abc") == "abc"
    assert parse_bearer("Token abc") is None
    assert parse_bearer(None) is None
    assert parse_bearer("Bearer ") is None


def test_verify_key() -> None:
    import hashlib

    assert verify_key("raw-key", None) is False
    assert verify_key(None, "stored") is False
    assert verify_key("", "") is False
    stored = hashlib.sha256(b"raw-key").hexdigest()
    assert verify_key("raw-key", stored) is True
    assert verify_key("other-key", stored) is False


def test_origin_exact_match() -> None:
    allowed = ["https://app.internal", "http://127.0.0.1:8000"]
    assert is_allowed_origin(None, allowed) is True
    assert is_allowed_origin("https://app.internal", allowed) is True
    assert is_allowed_origin("https://app.internal/", allowed) is True
    assert is_allowed_origin("http://127.0.0.1:8000", allowed) is True
    assert is_allowed_origin("https://app.internal.evil.example", allowed) is False
    assert is_allowed_origin("https://app.internal:8443", allowed) is False
    assert is_allowed_origin("http://app.internal", allowed) is False
    assert is_allowed_origin("https://example.com/app", allowed) is False
    assert is_allowed_origin("http://127.0.0.1.evil.example", allowed) is False


def test_origin_config_rejects_wildcard_and_suffixes() -> None:
    with pytest.raises(ValueError, match="allowed_origins"):
        validate_origin_config(["*"])
    with pytest.raises(ValueError, match="invalid origin"):
        validate_origin_config(["https://app.internal/path"])


def test_authenticate_request_uses_one_entry(monkeypatch, tmp_path) -> None:
    settings = make_settings(monkeypatch, tmp_path, auth_mode="dev")
    principal = authenticate_request(None, settings, authorization=None, origin=None)
    assert principal.name == dev_principal(settings).name

    enforced = make_settings(
        monkeypatch,
        tmp_path,
        auth_mode="enforced",
        allowed_origins=["https://app.internal"],
    )
    with pytest.raises(AuthDenied) as missing:
        authenticate_request(None, enforced, authorization=None, origin=None)
    assert missing.value.status == 401

    with pytest.raises(AuthDenied) as origin:
        authenticate_request(
            None,
            enforced,
            authorization="Bearer libref_x",
            origin="https://evil.example",
        )
    assert origin.value.code == "origin_forbidden"
