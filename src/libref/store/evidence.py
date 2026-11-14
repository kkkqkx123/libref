"""Evidence sufficiency: does the returned set actually answer the query?

Retrieval always returns its best k rows, so an empty result set is a weak proxy
for "the corpus holds no answer". This module reads the ranking instead, from two
coverages that cost no extra scoring:

- pool coverage: how much of the query appears anywhere in the candidate pool.
  A query whose topic the corpus never mentions leaves most units unmatched, and
  no amount of ranking can fix that.
- head coverage: how much of the query the best few bodies contain. It separates
  a pool that merely shares words from rows that speak to the question.

Two other candidates were measured on the benchmark and dropped: the gap between
the head score and the rest (it inverted on the full-text path, where junk
matches spread more than real ones) and lane agreement (no separation). Reranking
still matters, through the order it produces.

Coverage is lexical, so it only speaks about queries and corpora in a shared
script. When they share none — a Chinese query over an English corpus — the
answer is unknown rather than negative, and the verdict stays at weak instead of
refusing to answer.

The verdict is reported next to the results and never applied by filtering: a
caller must be able to tell "weak candidates" from "no candidates".
"""

from __future__ import annotations

from typing import Any

from libref.text.cjk import CJK_RUN_RE
from libref.text.query import query_units

HEAD = 3

_WEIGHTS = {"pool": 0.6, "head": 0.4}

# Reported value for a query whose script the corpus cannot match: the midpoint,
# which lands in the weak band rather than in the refusal band.
NEUTRAL = 0.5


def script_mismatch(query: str, pool: list[str]) -> bool:
    """A query whose script the corpus cannot match at all.

    When the query carries CJK but none of the candidate bodies does, the lexical
    lane is structurally unable to answer: downgrading it is the correct behavior,
    not a failure to find evidence. The reverse direction needs no flag: latin
    units still match the code and API names inside a CJK corpus.
    """
    if not CJK_RUN_RE.search(query or ""):
        return False
    return not CJK_RUN_RE.search(" ".join(pool))


def pool_texts(conn, ids: list[int]) -> dict[int, str]:
    """Body text of the candidate pool, which the coverage signals are read from."""
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, content FROM documents WHERE id IN ({marks})", ids
    ).fetchall()
    return {int(row[0]): str(row[1] or "") for row in rows}


def _units_for(query: str, pool: list[str]) -> list[str]:
    """Query units the corpus script can match at all."""
    units = query_units(query)
    if CJK_RUN_RE.search(" ".join(pool)):
        return units
    return [unit for unit in units if not CJK_RUN_RE.search(unit)]


def _share(texts: list[str], units: list[str]) -> float:
    lowered = " ".join(texts).lower()
    return sum(1 for unit in units if unit in lowered) / len(units)


def _strength(parts: dict[str, float]) -> float:
    total = sum(_WEIGHTS[name] for name in parts)
    weighted = sum(
        _WEIGHTS[name] * max(0.0, min(1.0, value)) for name, value in parts.items()
    )
    return weighted / total


def level_of(strength_value: float, strong: float, weak: float) -> str:
    """Three-way verdict; `weak` is reported but never gated."""
    if strength_value >= strong:
        return "answer"
    if strength_value >= weak:
        return "weak"
    return "none"


def assess(query: str, pool: list[str], head: list[str]) -> dict[str, Any]:
    """Coverage signals for one result set, with whether they mean anything here."""
    units = _units_for(query, pool)
    if not units:
        return {
            "pool": NEUTRAL,
            "head": NEUTRAL,
            "measurable": False,
            "strength": NEUTRAL,
        }
    parts = {"pool": _share(pool, units), "head": _share(head, units)}
    return {**parts, "measurable": True, "strength": _strength(parts)}


def verdict(
    query: str,
    results: list[dict[str, Any]],
    texts_by_id: dict[int, str],
    settings=None,
) -> dict[str, Any]:
    """Answerability fields for a search status map; empty results are never answerable."""
    from libref.config import setting_or_default

    if not results:
        return {"answerability": 0.0, "evidence": "none", "answerable": False}
    strong = float(setting_or_default(settings, "answer_strong_threshold"))
    weak = float(setting_or_default(settings, "answer_weak_threshold"))
    pool = [
        texts_by_id[int(row["id"])] for row in results if int(row["id"]) in texts_by_id
    ]
    head = [
        texts_by_id[int(row["id"])]
        for row in results[:HEAD]
        if int(row["id"]) in texts_by_id
    ]
    signal = assess(query, pool, head)
    level = (
        level_of(signal["strength"], strong, weak) if signal["measurable"] else "weak"
    )
    return {
        "answerability": round(float(signal["strength"]), 4),
        "evidence": level,
        "answerable": level != "none",
    }
