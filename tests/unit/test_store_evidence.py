"""Evidence sufficiency: coverages, verdicts and the unmeasurable case."""

from __future__ import annotations

import pytest

from libref.store.evidence import (
    NEUTRAL,
    assess,
    level_of,
    script_mismatch,
    verdict,
)

pytestmark = pytest.mark.unit


def _row(doc_id: int, score: float = 1.0) -> dict:
    return {"id": doc_id, "score": score}


def test_coverage_counts_query_units_present_in_the_text() -> None:
    parts = assess(
        "pool timeout settings",
        pool=["timeout settings"],
        head=["timeout"],
    )
    assert parts["pool"] == pytest.approx(2 / 3)
    assert parts["head"] == pytest.approx(1 / 3)


def test_absent_topic_covers_far_less_than_an_answered_one() -> None:
    query = "celery chord callback retry policy"
    answered = assess(
        query,
        pool=["retry policy for the client", "callback hooks", "chord"],
        head=["chord"],
    )
    absent = assess(
        query,
        pool=["the timeout of the client"],
        head=["the timeout of the client"],
    )
    assert answered["strength"] > absent["strength"]


def test_cjk_query_over_latin_corpus_is_unmeasurable_not_negative() -> None:
    """No shared script means coverage says nothing, so it must not vote to refuse."""
    signal = assess(
        "证书校验失败怎么关闭校验", pool=["ssl verify"], head=["ssl verify"]
    )
    assert signal["measurable"] is False
    assert signal["strength"] == NEUTRAL


def test_cjk_query_keeps_its_latin_units_when_the_corpus_is_latin() -> None:
    signal = assess(
        "Response 如何 decode gzip 解码",
        pool=["response decode gzip"],
        head=["response decode gzip"],
    )
    assert signal["measurable"] is True
    assert signal["strength"] > 0.0


def test_cjk_corpus_counts_cjk_units() -> None:
    signal = assess("证书校验", pool=["如何关闭证书校验"], head=["如何关闭证书校验"])
    assert signal["measurable"] is True
    assert signal["strength"] == 1.0


def test_script_mismatch_needs_cjk_on_the_query_side() -> None:
    assert script_mismatch("证书校验", ["ssl verify"]) is True
    assert script_mismatch("ssl verify", ["ssl verify"]) is False
    assert script_mismatch("证书校验", ["如何关闭证书校验"]) is False
    assert script_mismatch("", []) is False


def test_level_boundaries() -> None:
    assert level_of(0.9, strong=0.8, weak=0.5) == "answer"
    assert level_of(0.8, strong=0.8, weak=0.5) == "answer"
    assert level_of(0.5, strong=0.8, weak=0.5) == "weak"
    assert level_of(0.49, strong=0.8, weak=0.5) == "none"


def test_dispersed_coverage_is_discounted_but_concentrated_coverage_is_not() -> None:
    units = "alpha1 bravo1 charlie1 delta1 echo1 foxtrot1 golf1 hotel1"
    concentrated = assess(units, [units], [units])
    dispersed = assess(
        units,
        ["alpha1 bravo1", "charlie1 delta1", "echo1 foxtrot1", "golf1 hotel1"],
        ["alpha1 bravo1", "charlie1 delta1", "echo1 foxtrot1"],
    )
    assert concentrated["strength"] == pytest.approx(1.0)
    assert dispersed["concentration"] == pytest.approx(0.25)
    assert dispersed["strength"] < 0.5


def test_unmeasurable_signal_reports_a_concentration() -> None:
    signal = assess(
        "证书校验失败怎么关闭校验", pool=["ssl verify"], head=["ssl verify"]
    )
    assert signal["measurable"] is False
    assert signal["concentration"] == NEUTRAL


def test_cross_lingual_downgrade_skips_the_dispersion_penalty() -> None:
    query = "证书 timeout settings retry policy"
    pool = ["timeout", "settings", "retry", "policy"]
    signal = assess(query, pool, pool[:3])
    assert signal["concentration"] == pytest.approx(0.25)
    assert signal["strength"] == pytest.approx(0.9)


def test_verdict_refuses_when_topic_words_are_scattered_across_candidates() -> None:
    query = "webpack code splitting configuration rules plugins babel"
    words = query.split()
    out = verdict(
        query,
        [_row(index + 1) for index in range(len(words))],
        {index + 1: word for index, word in enumerate(words)},
        settings=None,
    )
    assert out["evidence"] == "none"


def test_verdict_without_results_is_never_answerable() -> None:
    out = verdict("anything", [], {})
    assert out == {"answerability": 0.0, "evidence": "none", "answerable": False}


def test_verdict_refuses_when_the_pool_does_not_cover_the_query() -> None:
    texts = {1: "the timeout of the client", 2: "status codes"}
    out = verdict(
        "celery chord callback retry policy", [_row(1), _row(2)], texts, settings=None
    )
    assert out["evidence"] == "none"
    assert out["answerable"] is False


def test_verdict_answers_when_the_pool_covers_the_query() -> None:
    texts = {1: "retry policy and callback", 2: "chord and retry"}
    out = verdict(
        "celery chord callback retry policy", [_row(1), _row(2)], texts, settings=None
    )
    assert out["evidence"] == "answer"
    assert out["answerable"] is True
