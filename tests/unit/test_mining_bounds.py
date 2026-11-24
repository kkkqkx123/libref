"""Bounds of the sequence-mining service entry."""

from __future__ import annotations

import pytest

from libref.errors import LibrefError
from libref.services.code.mining_refresh import (
    DEFAULT_MAXLEN,
    DEFAULT_MINSUP,
    mine_job_params,
    validate_mining_params,
)

pytestmark = pytest.mark.unit


def test_default_mining_params_are_accepted() -> None:
    validate_mining_params(DEFAULT_MINSUP, DEFAULT_MAXLEN, 2)


@pytest.mark.parametrize(
    ("minsup", "maxlen", "minlen"),
    [
        (1, 10, 2),
        (2, 10, 1),
        (2, 1, 2),
        (2, 10, 11),
        (2, DEFAULT_MAXLEN + 1, 2),
    ],
)
def test_unbounded_mining_params_are_rejected(
    minsup: int, maxlen: int, minlen: int
) -> None:
    with pytest.raises(LibrefError):
        validate_mining_params(minsup, maxlen, minlen)


def test_job_params_use_the_same_bounds() -> None:
    with pytest.raises(LibrefError):
        mine_job_params("demo", "0.1", minsup=1)
