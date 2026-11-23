"""Pure document-to-code alignment and consistency decisions.

Inputs are plain records assembled by services; outputs are candidate rows ready
for bulk persistence. Matching is deterministic and never invents an entity for
an ambiguous name.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from libref.text.ident import ident_snake_form

MATCH_SYMBOL = "symbol"
MATCH_SIGNATURE = "signature"
MATCH_FUZZY = "name_fuzzy"

_KIND_RANK = {MATCH_SYMBOL: 3, MATCH_SIGNATURE: 2, MATCH_FUZZY: 1}
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


@dataclass
class ApiIndex:
    """Version entity lookup with ambiguity tracked separately from exact names."""

    exact: dict[str, int] = field(default_factory=dict)
    fuzzy: dict[str, int] = field(default_factory=dict)
    ambiguous: set[str] = field(default_factory=set)
    names: dict[int, str] = field(default_factory=dict)


def _short(name: str) -> str:
    return str(name or "").rsplit(".", 1)[-1]


def _norm(name: str) -> str:
    return ident_snake_form(_short(name))


def build_api_index(entities: list[dict[str, Any]]) -> ApiIndex:
    """Index qualified names and normalized short names; ambiguous keys are dropped."""
    index = ApiIndex()
    exact_counts: dict[str, set[int]] = {}
    fuzzy_counts: dict[str, set[int]] = {}
    for entity in entities:
        entity_id = int(entity["id"])
        name = str(entity.get("qualified_name") or "")
        if not name:
            continue
        index.names[entity_id] = name
        for key in (name, f"signature:{name}"):
            exact_counts.setdefault(key, set()).add(entity_id)
        short = _short(name)
        for key in (f"short:{short}", f"norm:{_norm(name)}"):
            fuzzy_counts.setdefault(key, set()).add(entity_id)
    for key, ids in exact_counts.items():
        if len(ids) == 1:
            index.exact[key] = next(iter(ids))
        else:
            index.ambiguous.add(key)
    for key, ids in fuzzy_counts.items():
        if len(ids) == 1:
            index.fuzzy[key] = next(iter(ids))
        else:
            index.ambiguous.add(key)
    return index


def _tokens(content: str) -> set[str]:
    return {match.group(0) for match in _WORD_RE.finditer(str(content or ""))}


def _norm_tokens(content: str) -> set[str]:
    return {ident_snake_form(token) for token in _tokens(content)}


def _code_hashes(block: dict[str, Any]) -> set[str]:
    return {str(item) for item in (block.get("code_hashes") or []) if item}


def align_documents(
    blocks: list[dict[str, Any]],
    index: ApiIndex,
    snippet_calls: list[dict[str, Any]],
    *,
    min_confidence: float = 0.4,
    limit_per_block: int = 50,
) -> list[dict[str, Any]]:
    """Match one document block to version entities using ordered evidence kinds."""
    by_url: dict[str, list[dict[str, Any]]] = {}
    for call in snippet_calls:
        url = str(call.get("url") or "")
        if url:
            by_url.setdefault(url, []).append(call)
    out: list[dict[str, Any]] = []
    for block in blocks:
        document_id = int(block["document_id"])
        content = str(block.get("content") or "")
        words = _tokens(content)
        norms = _norm_tokens(content)
        hashes = _code_hashes(block)
        found: dict[int, dict[str, Any]] = {}

        def offer(
            entity_id: int,
            kind: str,
            confidence: float,
            evidence: dict[str, Any],
            found=found,
            document_id=document_id,
        ) -> None:
            if entity_id <= 0 or confidence < min_confidence:
                return
            previous = found.get(entity_id)
            if (
                previous is not None
                and _KIND_RANK[previous["match_kind"]] >= _KIND_RANK[kind]
            ):
                return
            found[entity_id] = {
                "document_id": document_id,
                "api_entity_id": entity_id,
                "match_kind": kind,
                "confidence": round(min(1.0, confidence), 4),
                "evidence": evidence,
            }

        url = str(block.get("url") or "")
        for call in by_url.get(url, []):
            entity_id = int(call.get("api_entity_id") or 0)
            name = str(call.get("callee") or "")
            call_hash = str(call.get("code_hash") or "")
            tied = bool(call_hash and call_hash in hashes) or (
                bool(name) and name in words and not hashes
            )
            if not tied or entity_id <= 0:
                continue
            base = (
                1.0 if str(call.get("confidence", "EXTRACTED")) == "EXTRACTED" else 0.8
            )
            offer(
                entity_id,
                MATCH_SYMBOL,
                base,
                {
                    "sample_id": str(call.get("sample_id") or ""),
                    "code_hash": call_hash,
                    "source_role": str(call.get("source_role") or "doc_snippet"),
                },
            )
        for name, entity_id in index.exact.items():
            if name.startswith("signature:"):
                continue
            if name in words:
                offer(entity_id, MATCH_SIGNATURE, 0.8, {"name": name})
        for key, entity_id in index.fuzzy.items():
            candidate = key.split(":", 1)[1]
            if (key.startswith("norm:") and candidate in norms) or (
                key.startswith("short:") and candidate in words
            ):
                offer(entity_id, MATCH_FUZZY, 0.4, {"name": candidate})
        ordered = sorted(
            found.values(),
            key=lambda row: (
                -row["confidence"],
                row["match_kind"],
                row["api_entity_id"],
            ),
        )
        out.extend(ordered[:limit_per_block])
    return out


def _is_subsequence(sequence: list[str], pattern: list[str]) -> bool:
    """Patterns are order-preserving subsequences, as in the sequence miner."""
    pos = 0
    for name in sequence:
        if pos < len(pattern) and name == pattern[pos]:
            pos += 1
    return pos == len(pattern)


def pattern_hash(pattern: list[str]) -> str:
    """Stable identity for a pattern independent of its transient row id."""
    raw = json.dumps(list(pattern), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cover_patterns(
    blocks: list[dict[str, Any]],
    patterns: list[dict[str, Any]],
    *,
    min_support: int = 2,
) -> list[dict[str, Any]]:
    """Cover mined patterns by document example sequences or prose mentions."""
    out: list[dict[str, Any]] = []
    for pattern_row in patterns:
        names = [str(name) for name in (pattern_row.get("pattern") or []) if name]
        support = int(pattern_row.get("support") or 0)
        if not names or support < min_support:
            continue
        digest = str(pattern_row.get("pattern_hash") or pattern_hash(names))
        for block in blocks:
            document_id = int(block["document_id"])
            sequence = [
                str(name) for name in (block.get("call_sequence") or []) if name
            ]
            if sequence and _is_subsequence(sequence, names):
                kind = "example_code"
            elif all(name in _tokens(block.get("content") or "") for name in names):
                kind = "prose"
            else:
                continue
            out.append(
                {
                    "document_id": document_id,
                    "pattern_hash": digest,
                    "pattern": names,
                    "covered_kind": kind,
                    "support": support,
                }
            )
    return out


def consistency_evidence_hash(parts: list[Any]) -> str:
    """Fingerprint of the deterministic inputs behind one consistency verdict."""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compare_consistency(
    blocks: list[dict[str, Any]],
    links: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    *,
    min_support: int = 2,
) -> list[dict[str, Any]]:
    """Derive drift verdicts from deprecated markers and uncovered pattern entities."""
    linked: dict[int, set[int]] = {}
    for link in links:
        linked.setdefault(int(link["document_id"]), set()).add(
            int(link["api_entity_id"])
        )
    covered: dict[int, set[str]] = {}
    pattern_names: dict[str, set[str]] = {}
    for row in coverage:
        covered.setdefault(int(row["document_id"]), set()).add(str(row["pattern_hash"]))
        pattern_names[str(row["pattern_hash"])] = {
            str(name) for name in (row.get("pattern") or []) if name
        }
    out: list[dict[str, Any]] = []
    for block in blocks:
        document_id = int(block["document_id"])
        deprecated = bool(block.get("deprecated"))
        entities = linked.get(document_id, set())
        hashes = covered.get(document_id, set())
        evidence = {
            "deprecated": deprecated,
            "entities": sorted(entities),
            "coverage": sorted(hashes),
            "code_hash": sorted(_code_hashes(block)),
        }
        digest = consistency_evidence_hash(
            [
                document_id,
                evidence["deprecated"],
                evidence["entities"],
                evidence["coverage"],
            ]
        )
        if deprecated:
            verdict = "drift"
        elif hashes:
            verdict = "match"
        elif block.get("call_sequence") and pattern_names and not entities:
            verdict = "drift"
        else:
            verdict = "unknown"
        out.append(
            {
                "document_id": document_id,
                "facet": "deprecated_usage" if deprecated else "pattern_alignment",
                "verdict": verdict,
                "evidence_hash": digest,
                "evidence": evidence,
            }
        )
    return out
