"""Classification of documentation snippet language and parse outcomes."""

from __future__ import annotations

from libref.astminer.models import (
    SNIPPET_STATUS_AMBIGUOUS,
    SNIPPET_STATUS_FAILED,
    SNIPPET_STATUS_NON_TARGET,
    SNIPPET_STATUS_OK,
    SNIPPET_STATUS_PARTIAL,
    SNIPPET_STATUS_UNLABELED,
    SNIPPET_STATUS_UNSUPPORTED,
    ParseEvidence,
)


def classify_snippet_status(
    evidence: ParseEvidence | None,
    *,
    language: str = "",
    raw_language: str = "",
    language_source: str = "",
    configured: bool = False,
    usable_candidates: int = 0,
    target_calls: int = 0,
) -> str:
    """Classify one snippet from language evidence and parse output.

    Candidate languages are accepted only when exactly one of them produces
    call or entity evidence. Error nodes alone never turn a valid example into
    a hard failure: they only downgrade an evidenced parse to ``partial``.
    """
    if language_source == "candidate":
        if usable_candidates == 0:
            return SNIPPET_STATUS_FAILED
        if usable_candidates > 1:
            return SNIPPET_STATUS_AMBIGUOUS
    if not language and not raw_language:
        return SNIPPET_STATUS_UNLABELED
    if not configured:
        return SNIPPET_STATUS_UNSUPPORTED
    if evidence is None:
        return SNIPPET_STATUS_FAILED
    if target_calls > 0:
        return SNIPPET_STATUS_PARTIAL if evidence.has_error else SNIPPET_STATUS_OK
    if evidence.call_count > 0:
        return SNIPPET_STATUS_NON_TARGET
    if evidence.has_api_evidence:
        return SNIPPET_STATUS_PARTIAL if evidence.has_error else SNIPPET_STATUS_OK
    if evidence.alias_count > 0 or evidence.has_error:
        return SNIPPET_STATUS_PARTIAL
    return SNIPPET_STATUS_NON_TARGET
