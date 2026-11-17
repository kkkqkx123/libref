"""Pre-release detection is per-registry: one substring rule cannot cover all four.

Registries spell pre-releases differently — npm/crates/nuget after a hyphen,
PyPI with PEP 440 segment letters, Maven with free-form tokens, Go with
pseudo-versions — so these cases pin the dispatch and the token-boundary fallback
that replaced the old substring search.
"""

from __future__ import annotations

import pytest

from libref.crawler.registry import best_effort_stable, is_prerelease

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("version", "ecosystem", "expected"),
    [
        # PEP 440: segment letters, no hyphen involved.
        ("1.0.0a1", "pypi", True),
        ("1.0.0b2", "pypi", True),
        ("1.0.0rc1", "pypi", True),
        ("1.0.0.dev3", "pypi", True),
        ("1.0.0-alpha", "pypi", True),
        ("1.0.0.post1", "pypi", False),
        ("1.0.0", "pypi", False),
        # Maven: milestone and early-access tokens.
        ("3.4.5-M1", "maven", True),
        ("2.0.0-EA", "maven", True),
        ("1.2.3-SNAPSHOT", "maven", True),
        ("1.0.0.Final", "maven", False),
        ("2.1.0", "maven", False),
        # SemVer registries: any hyphen suffix is a pre-release identifier.
        ("4.0.0-beta.2", "npm", True),
        ("0.0.0-20240101", "npm", True),
        ("13.0.1-patch1", "nuget", True),
        ("1.0.0", "nuget", False),
        ("1.0.0+build.5", "npm", False),
        # Go: hyphen suffix plus pseudo-versions, build metadata ignored.
        ("v1.2.3-pre", "go", True),
        ("v0.0.0-20240101120000-abcdef123456", "go", True),
        ("v1.2.3", "go", False),
        ("v1.0.0+incompatible", "go", False),
        # Token fallback: whole tokens only, so ordinary words do not misfire.
        ("1.0.0-compressed", None, False),
        ("1.0.0-deprecated", None, False),
        ("1.0.0-prefix", None, False),
        ("1.0.0-beta1", None, True),
        ("1.0.0-rc2", None, True),
        ("1.0.0-canary.3", None, True),
        ("", None, False),
    ],
)
def test_prerelease_dispatch(
    version: str, ecosystem: str | None, expected: bool
) -> None:
    assert is_prerelease(version, ecosystem) is expected


def test_nuget_keeps_its_hyphen_rule() -> None:
    """NuGet follows SemVer; the old dedicated helper is folded into the dispatch."""
    assert is_prerelease("13.0.1-patch1", "nuget") is True
    assert is_prerelease("13.0.1", "nuget") is False


def test_stable_pick_passes_ecosystem_through() -> None:
    """Without the ecosystem the pypi segment letters would look like releases."""
    versions = ["1.0.0a1", "1.0.0"]
    assert best_effort_stable(versions, ecosystem="pypi") == "1.0.0"
    # Only pre-releases present: the fallback still returns something usable.
    assert best_effort_stable(["1.0.0a1", "1.1.0b1"], ecosystem="pypi") == "1.1.0b1"
    assert (
        best_effort_stable(["1.0.0a1", "1.1.0b1"], include_prerelease=True) == "1.1.0b1"
    )
