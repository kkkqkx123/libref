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
MATCH_CONTEXT = "context_symbol"
MATCH_FUZZY = "name_fuzzy"

_KIND_RANK = {
    MATCH_SYMBOL: 4,
    MATCH_SIGNATURE: 3,
    MATCH_CONTEXT: 2,
    MATCH_FUZZY: 1,
}
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
_SENTENCE_RE = re.compile(r"[^.!?。！？\n]+")
_DEPRECATION_WORDS = (
    "deprecated",
    "deprecation",
    "no longer supported",
    "removed in",
    "has been removed",
    "have been removed",
    "will be removed",
    "已移除",
    "被移除",
    "废弃",
    "弃用",
    "不再支持",
)
_NEGATION_WORDS = (
    "not deprecated",
    "will not be deprecated",
    "won't be deprecated",
    "不会废弃",
    "不会弃用",
    "不会被移除",
    "不会，",
    "不会,",
    "没有任何计划",
    "没有计划",
    "no plans",
)
_EXCLUDED_WORDS_RE = re.compile(
    r"\b(ipv4|ipv6|tls|ssl|site-local|rfc)\b", re.IGNORECASE
)
_EXCLUDED_PHRASES = (
    "deprecation warning",
    "changelog",
    "lifecycle states",
    "feature states",
)
_CODE_OBJECT_RE = re.compile(
    r"`[A-Za-z_][A-Za-z0-9_.:()/-]{1,64}`"
    r"|\b[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+(?:\(\))?\b"
    r"|\b[A-Za-z_][A-Za-z0-9_]*\(\)\b"
    r"|\b[a-z][a-z0-9]*_[a-z0-9_]+\b"
    r"|\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\b"
    r"|\b[a-z][a-z0-9_]*="
)
_HEADING_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*|[\u4e00-\u9fff]{2,}")
_CALLOUT_RE = re.compile(r":::\s*(danger|warning|caution|注意|警告)", re.IGNORECASE)
_GENERIC_OBJECTS = frozenset(
    {
        "deprecated",
        "deprecation",
        "warning",
        "warnings",
        "with",
        "states",
        "state",
        "lifecycle",
        "features",
        "特性",
        "版本",
        "发布",
        "状态",
        "政策",
        "支持",
        "移除",
    }
)


@dataclass
class ApiIndex:
    """Version entity lookup with scoped candidates for ambiguous short names."""

    exact: dict[str, int] = field(default_factory=dict)
    fuzzy: dict[str, list[int]] = field(default_factory=dict)
    ambiguous: dict[str, list[int]] = field(default_factory=dict)
    ambiguous_exact: set[str] = field(default_factory=set)
    names: dict[int, str] = field(default_factory=dict)
    parents: dict[int, str] = field(default_factory=dict)


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
        index.parents[entity_id] = name.rpartition(".")[0]
        for key in (name, f"signature:{name}"):
            exact_counts.setdefault(key, set()).add(entity_id)
        short = _short(name)
        for key in (f"short:{short}", f"norm:{_norm(name)}"):
            fuzzy_counts.setdefault(key, set()).add(entity_id)
    for key, ids in exact_counts.items():
        if len(ids) == 1:
            index.exact[key] = next(iter(ids))
        else:
            index.ambiguous_exact.add(key)
    for key, ids in fuzzy_counts.items():
        ordered = sorted(ids)
        if len(ordered) == 1:
            index.fuzzy[key] = ordered
        else:
            index.ambiguous[key] = ordered
    return index


def _context_tokens(block: dict[str, Any]) -> set[str]:
    """Words available from the block body and its heading path."""
    text = "\n".join(str(block.get(key) or "") for key in ("content", "path", "title"))
    out: set[str] = set()
    for token in _WORD_RE.findall(text):
        out.add(token)
        out.update(part for part in token.split(".") if part)
    return out


def _contextual_candidate(
    candidates: list[int], index: ApiIndex, words: set[str]
) -> int:
    """Pick one ambiguous entity when a distinctive parent name appears nearby."""
    parent_parts = {
        entity_id: {
            part for part in str(index.parents.get(entity_id) or "").split(".") if part
        }
        for entity_id in candidates
    }
    shared: set[str] = (
        set.intersection(*parent_parts.values()) if parent_parts else set()
    )
    best: list[tuple[int, int]] = []
    for entity_id, parts in parent_parts.items():
        score = len((parts - shared) & words)
        if score:
            best.append((score, entity_id))
    if not best:
        return 0
    best.sort(key=lambda item: (-item[0], item[1]))
    return best[0][1] if len(best) == 1 or best[0][0] > best[1][0] else 0


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
        context_words = _context_tokens(block)
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
            bind_kind = str(call.get("bind_kind") or "ordinal")
            evidence = {
                "sample_id": str(call.get("sample_id") or ""),
                "code_hash": call_hash,
                "bind_kind": bind_kind,
                "source_role": str(call.get("source_role") or "doc_snippet"),
            }
            if entity_id <= 0:
                continue
            if bind_kind == "name":
                if name in context_words or _short(name) in context_words:
                    offer(entity_id, MATCH_CONTEXT, 0.6, evidence)
                continue
            tied = bool(call_hash and call_hash in hashes) or (
                bool(name) and name in words and not hashes
            )
            if not tied:
                continue
            base = (
                1.0 if str(call.get("confidence", "EXTRACTED")) == "EXTRACTED" else 0.8
            )
            offer(entity_id, MATCH_SYMBOL, base, evidence)
        for name, entity_id in index.exact.items():
            if name.startswith("signature:"):
                continue
            if name in words:
                offer(entity_id, MATCH_SIGNATURE, 0.8, {"name": name})
        for key, entity_ids in index.fuzzy.items():
            candidate = key.split(":", 1)[1]
            hit = (key.startswith("norm:") and candidate in norms) or (
                key.startswith("short:") and candidate in words
            )
            if hit:
                offer(entity_ids[0], MATCH_FUZZY, 0.4, {"name": candidate})
        for key, entity_ids in index.ambiguous.items():
            candidate = key.split(":", 1)[1]
            hit = (key.startswith("norm:") and candidate in norms) or (
                key.startswith("short:") and candidate in words
            )
            if not hit:
                continue
            chosen = _contextual_candidate(entity_ids, index, context_words)
            if chosen:
                offer(
                    chosen,
                    MATCH_CONTEXT,
                    0.6,
                    {"name": candidate, "scope": index.parents.get(chosen, "")},
                )
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


def link_feature_nodes(
    links: list[dict[str, Any]],
    entity_names: dict[int, str],
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach aligned entities to feature-tree nodes and their parent chains."""
    parents: dict[str, str] = {}
    for node in nodes:
        path = str(node.get("path") or "")
        if path:
            parents[path] = str(node.get("parent") or "")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for link in links:
        document_id = int(link["document_id"])
        entity_id = int(link["api_entity_id"])
        path = str(entity_names.get(entity_id) or "")
        visited: set[str] = set()
        while path and path in parents and path not in visited:
            key = (document_id, entity_id, path)
            if key not in seen:
                rows.append(
                    {
                        "document_id": document_id,
                        "api_entity_id": entity_id,
                        "node_path": path,
                    }
                )
                seen.add(key)
            visited.add(path)
            path = parents[path]
    rows.sort(
        key=lambda row: (
            row["document_id"],
            row["api_entity_id"],
            row["node_path"],
        )
    )
    return rows


def consistency_evidence_hash(parts: list[Any]) -> str:
    """Fingerprint of the deterministic inputs behind one consistency verdict."""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sentence_objects(sentence: str) -> set[str]:
    return {
        match.group(0).strip("`").lower()
        for match in _CODE_OBJECT_RE.finditer(str(sentence or ""))
    }


def _heading_objects(path: str, title: str) -> set[str]:
    out: set[str] = set()
    for raw in (path, title):
        for segment in str(raw or "").split("/"):
            text = re.sub(r"\{#[^}]*\}", "", segment)
            text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
            text = text.replace("`", "").replace("\\_", "_").strip()
            out.update(_sentence_objects(text))
            for match in _HEADING_TOKEN_RE.findall(text):
                token = match.lower()
                if any(word in token for word in _GENERIC_OBJECTS) or any(
                    word in token for word in _DEPRECATION_WORDS
                ):
                    continue
                if len(token) >= 4 or re.search(r"[\u4e00-\u9fff]", token):
                    out.add(token)
    return out


def _matches_object(obj: str, text: str) -> bool:
    """Whether a heading object is named by a deprecation sentence."""
    if obj in text:
        return True
    tokens = [token.lower() for token in _HEADING_TOKEN_RE.findall(text)]
    if re.search(r"[\u4e00-\u9fff]", obj):
        return any(obj in token or token in obj for token in tokens)
    prefix = obj[: min(6, len(obj))]
    return any(token.startswith(prefix) for token in tokens if len(token) >= 4)


def _bound_deprecation(
    content: str,
    entities: set[int],
    names: dict[int, str],
    *,
    path: str = "",
    title: str = "",
) -> bool:
    """Bind a deprecation sentence to an entity, code object, or scoped heading."""
    labels: set[str] = set()
    for entity_id in entities:
        name = str(names.get(entity_id) or "").lower()
        if not name:
            continue
        labels.add(name)
        short = name.rpartition(".")[2]
        if len(short) >= 4:
            labels.add(short)
    text = str(content or "")
    lowered = text.lower()
    negated = any(word in lowered for word in _NEGATION_WORDS)
    heading = _heading_objects(path, title)
    heading_code = {obj for obj in heading if _CODE_OBJECT_RE.fullmatch(obj)}
    callout = bool(_CALLOUT_RE.search(text))
    heading_deprecated = any(
        word in f"{path} {title}".lower() for word in _DEPRECATION_WORDS
    )
    lines = text.splitlines()
    for sentence in _SENTENCE_RE.findall(text):
        low = sentence.lower()
        if not any(word in low for word in _DEPRECATION_WORDS):
            continue
        if any(word in low for word in _NEGATION_WORDS):
            continue
        if _EXCLUDED_WORDS_RE.search(low):
            continue
        line = next((item for item in lines if sentence.strip() in item), sentence)
        code = _sentence_objects(sentence) | _sentence_objects(line)
        heading_match = (
            {
                obj
                for obj in heading
                if obj not in heading_code and _matches_object(obj, low)
            }
            if heading_deprecated or callout
            else set()
        )
        if negated and not code:
            continue
        if (
            any(phrase in low for phrase in _EXCLUDED_PHRASES)
            and not heading_match
            and not heading_code
            and not code
        ):
            continue
        if any(label in low for label in labels):
            return True
        if code or heading_code or heading_match or (callout and heading):
            return True
    return False


def compare_consistency(
    blocks: list[dict[str, Any]],
    links: list[dict[str, Any]],
    coverage: list[dict[str, Any]],
    *,
    entity_names: dict[int, str],
    min_support: int = 2,
) -> list[dict[str, Any]]:
    """Derive drift verdicts from entity-bound deprecation and pattern coverage."""
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
        bound = _bound_deprecation(
            str(block.get("content") or ""),
            entities,
            entity_names,
            path=str(block.get("path") or ""),
            title=str(block.get("title") or ""),
        )
        evidence = {
            "deprecated": deprecated,
            "deprecation_bound": bound,
            "deprecation_scope": str(block.get("path") or ""),
            "local_objects": sorted(
                _sentence_objects(str(block.get("content") or ""))
                | _heading_objects(
                    str(block.get("path") or ""), str(block.get("title") or "")
                )
            ),
            "entities": sorted(entities),
            "coverage": sorted(hashes),
            "code_hash": sorted(_code_hashes(block)),
        }
        digest = consistency_evidence_hash(
            [
                document_id,
                evidence["deprecated"],
                evidence["deprecation_bound"],
                evidence["local_objects"],
                evidence["entities"],
                evidence["coverage"],
            ]
        )
        if deprecated and bound:
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
