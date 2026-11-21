"""The shared error value: one table, one payload, deterministic exit codes."""

from __future__ import annotations

import json

import pytest

from libref.errors import (
    ALL_CODES,
    COARSE_CODES,
    ErrorPayload,
    LibrefError,
    coarse,
    coarse_field,
    exit_code_for,
    invalid_argument,
    normalize,
    sanitize,
    status_for,
)

pytestmark = pytest.mark.unit


def test_payload_omits_absent_members_and_stays_one_line() -> None:
    error = invalid_argument("limit must be between 1 and 100", field="limit")
    assert error.payload() == {
        "error": "invalid_argument",
        "message": "limit must be between 1 and 100",
        "field": "limit",
    }
    text = error.to_json()
    assert "\n" not in text and '", "' not in text and '": ' not in text
    assert str(error) == text
    assert ErrorPayload(**json.loads(text)).error == "invalid_argument"


def test_every_code_has_a_status_and_an_exit_status() -> None:
    for code in ALL_CODES:
        assert 400 <= status_for(code) <= 503
        assert exit_code_for(code) in (1, 2)


def test_caller_mistakes_exit_two_and_outages_exit_one() -> None:
    assert exit_code_for("invalid_argument") == 2
    assert exit_code_for("forbidden") == 2
    assert exit_code_for("not_found") == 1
    assert exit_code_for("unavailable") == 1


def test_variants_collapse_with_the_field_they_encoded() -> None:
    assert coarse("invalid_limit") == "invalid_argument"
    assert coarse_field("invalid_limit") == "limit"
    assert coarse("invalid_ecosystem") == "invalid_argument"
    assert coarse_field("invalid_ecosystem") == "ecosystem"
    for code in COARSE_CODES:
        assert coarse(code) == code


def test_sanitize_flattens_and_truncates() -> None:
    assert sanitize("a\n  b\tc") == "a b c"
    long = sanitize("x" * 400)
    assert len(long) <= 160 and long.endswith("...")


def test_value_error_becomes_a_caller_mistake() -> None:
    found = normalize(ValueError("limit must be positive"))
    assert found is not None
    assert found.code == "invalid_argument"
    assert found.message == "limit must be positive"


def test_index_request_conflict_maps_to_its_own_code() -> None:
    from libref.services.ops.index_requests import IndexRequestError

    found = normalize(IndexRequestError("already queued", code="conflict"))
    assert found is not None
    assert found.code == "index_request_conflict"
    invalid = normalize(IndexRequestError("bad payload"))
    assert invalid is not None and invalid.code == "invalid_argument"


def test_registry_failure_keeps_follow_ups() -> None:
    from libref.crawler.registry import RegistryError

    found = normalize(RegistryError("retryable", "registry unreachable", {"name": "x"}))
    assert found is not None
    assert found.code == "unavailable"
    assert found.hint
    assert found.next_actions


def test_unknown_exceptions_are_not_ours() -> None:
    assert normalize(RuntimeError("boom")) is None


def test_a_code_outside_the_table_is_rejected() -> None:
    with pytest.raises(ValueError):
        LibrefError("no_such_code", "x")  # type: ignore[arg-type]
