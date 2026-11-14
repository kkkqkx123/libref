"""Site-stated version signals: pure helpers shared by production and bench.

Reads only what the site says about itself: a version-looking path segment,
the canonical URL, or published version metadata. Never consults the registry.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

VERSION_STREAMS = frozenset(
    {"latest", "stable", "dev", "develop", "main", "master", "next", "current"}
)
_VERSION_TOKEN = re.compile(r"^v?\d+(?:\.\d+){0,2}(?:\.x)?$", re.IGNORECASE)
UNVERSIONED = "unversioned"


def version_like(segment: str) -> bool:
    """A path segment that names a release or a documentation stream."""
    return segment.lower() in VERSION_STREAMS or bool(_VERSION_TOKEN.match(segment))


def segments_of(url: str) -> list[str]:
    return [part for part in urlsplit(url).path.split("/") if part]


def evidence_from_paths(urls: list[str]) -> dict[str, str] | None:
    """Version segment shared by the crawled pages."""
    pages = [{"url": url} for url in urls]
    if not pages:
        return None
    counts: dict[tuple[int, str], int] = {}
    samples: dict[tuple[int, str], str] = {}
    for page in pages:
        for index, segment in enumerate(segments_of(str(page["url"]))[:3]):
            key = (index, segment)
            counts[key] = counts.get(key, 0) + 1
            samples.setdefault(key, str(page["url"]))
    floor = max(2, -(-len(pages) * 4 // 5))
    for (index, segment), hits in sorted(counts.items()):
        if hits >= floor and version_like(segment):
            return {
                "value": segment,
                "signal": f"url-path[{index}]",
                "sample_url": samples[(index, segment)],
            }
    return None


def majority_evidence(urls: list[str]) -> dict[str, str]:
    """Majority vote over crawled page URLs; unversioned when nothing states a version."""
    found = evidence_from_paths(urls)
    if found:
        return found
    return {"value": UNVERSIONED, "signal": "none", "sample_url": ""}


def compare_versions(requested: str, observed: str) -> str:
    """Compare the requested version against the site-stated one.

    Returns one of ok, warn, or fail. A stream name against a number is only
    a warning; two different concrete markers are a failure.
    """
    want = (requested or "").strip()
    seen = (observed or "").strip()
    if not want or not seen:
        return "warn"
    if want == seen:
        return "ok"
    if seen == UNVERSIONED or want == UNVERSIONED:
        return "warn"
    want_stream = want.lower() in VERSION_STREAMS
    seen_stream = seen.lower() in VERSION_STREAMS
    if want_stream != seen_stream:
        return "warn"
    return "fail"
