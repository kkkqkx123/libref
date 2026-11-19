"""HTTP boundary timestamp normalization; storage formats stay unchanged."""

from __future__ import annotations

import pytest

from libref.services.infra.timestamps import epoch_utc, iso_utc

pytestmark = pytest.mark.unit


def test_iso_utc_normalizes_storage_formats() -> None:
    assert iso_utc("2026-11-18 01:02:03") == "2026-11-18T01:02:03Z"
    assert iso_utc("2026-11-18T01:02:03+00:00") == "2026-11-18T01:02:03Z"
    assert iso_utc(1794963723.0) == "2026-11-18T01:02:03Z"
    assert iso_utc(None) is None
    assert iso_utc("") == ""


def test_epoch_utc_accepts_iso_input() -> None:
    assert epoch_utc("2026-11-18T01:02:03Z") == 1794963723.0
    assert epoch_utc(None) is None
