"""Pattern-level semantic induction: prompt assembly and payload validation.

Blueprint ref/EpiCoder (research reference): sampling-based feature-tree
induction and frequency pruning. Only the induction-at-aggregate-level idea
is borrowed here; per-file extraction with a model is never used. Structural
facts stay with tree-sitter; this module only shapes model inputs and checks
model outputs. Pure in-memory, no database and no network.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

__all__ = [
    "ClusterInductionInput",
    "ClusterLabel",
    "InductionInput",
    "PatternLabel",
    "build_cluster_prompt",
    "build_label_prompt",
    "cluster_key",
    "parse_cluster_payload",
    "parse_label_payload",
    "pattern_hash",
]


class InductionInput(BaseModel):
    pattern: list[str] = Field(min_length=1)
    support: int = Field(ge=1)
    arg_shapes: list[dict[str, Any]] = Field(default_factory=list)
    siblings: list[str] = Field(default_factory=list)
    scope: str = Field(default="")
    signatures: dict[str, str] = Field(default_factory=dict)
    source_hint: str = Field(default="")


class PatternLabel(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=500)
    category_path: list[str] = Field(min_length=1)
    workflow: str = Field(default="", max_length=500)
    related_apis: list[str] = Field(default_factory=list)

    @field_validator("category_path")
    @classmethod
    def _clean_path(cls, value: list[str]) -> list[str]:
        cleaned = [str(p).strip() for p in value if str(p).strip()]
        if not cleaned:
            raise ValueError("category_path is empty")
        if len(cleaned) > 4:
            raise ValueError("category_path is too deep")
        return cleaned

    @field_validator("related_apis")
    @classmethod
    def _clean_apis(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in seen:
                seen.append(text)
        return seen[:8]


def pattern_hash(pattern: list[str]) -> str:
    blob = json.dumps(list(pattern), ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha1(blob).hexdigest()


def cluster_key(members: list[str]) -> str:
    blob = json.dumps(sorted(set(members)), ensure_ascii=False).encode()
    return hashlib.sha1(blob).hexdigest()


class ClusterInductionInput(BaseModel):
    members: list[str] = Field(min_length=1)
    representative: str = Field(min_length=1)
    size: int = Field(ge=1)
    siblings: list[str] = Field(default_factory=list)
    scope: str = Field(default="")
    source_hint: str = Field(default="")
    source_roles: list[str] = Field(default_factory=list)


class ClusterLabel(PatternLabel):
    source_note: str = Field(min_length=1, max_length=500)


def build_cluster_prompt(item: ClusterInductionInput) -> list[dict[str, str]]:
    """Prompt messages for naming one aggregated API group."""
    members = ", ".join(sorted(set(item.members)))
    siblings = ", ".join(sorted(set(item.siblings))[:6])
    allowed = sorted(set(item.members) | {s for s in item.siblings if s})
    scope_segment = f"Scope: {item.scope.strip()}. " if item.scope.strip() else ""
    roles = ", ".join(r for r in item.source_roles if r)
    roles_segment = f"Corpus roles: {roles}. " if roles else ""
    source_segment = (
        f"Source context: {item.source_hint.strip()}. "
        "Treat provenance as evidence, not as an official recommendation. "
        if item.source_hint.strip()
        else ""
    )
    user = (
        "Name and describe this group of APIs that are used together. "
        f"{scope_segment}{roles_segment}{source_segment}"
        f"Members: {members}. "
        f"Representative: {item.representative}. Size: {item.size}. "
        f"Co-used APIs: {siblings or 'none'}. "
        f"Allowed related APIs: {', '.join(allowed) if allowed else 'none'}. "
        "Reply with JSON only: "
        '{"name": "...", "summary": "...", '
        '"category_path": ["...", "..."], '
        '"workflow": "...", "related_apis": ["..."], '
        '"source_note": "..."}. '
        "Derive category words from the API dotted names, coarse to fine, at most 4 levels. "
        "Related APIs must be chosen from the allowed APIs only. "
        "Source note must state in plain words which corpus the group comes from "
        "and that it is observed usage, not an official recommendation."
    )
    return [
        {
            "role": "system",
            "content": "You label mined API groups with short stable names. Reply with JSON only.",
        },
        {"role": "user", "content": user},
    ]


def parse_cluster_payload(
    raw: str, allowed_apis: set[str], entity_names: set[str]
) -> ClusterLabel:
    """Parse one group-label reply; related APIs must exist as version entities."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"cluster label payload is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError("cluster label payload must be an object")
    label = ClusterLabel.model_validate(data)
    unknown = [a for a in label.related_apis if a not in allowed_apis]
    if unknown:
        raise ValueError(f"related APIs are outside the version: {unknown}")
    outside = [a for a in label.related_apis if a not in entity_names]
    if outside:
        raise ValueError(f"related APIs are not version entities: {outside}")
    markers = (
        "<unit",
        "</unit",
        "<materials",
        "</materials",
        "<excerpt",
        "<hit",
        "<call",
        "<gap",
        "<reference",
        "<omitted",
    )
    checked = [
        label.name,
        label.summary,
        label.workflow,
        label.source_note,
        *label.category_path,
        *label.related_apis,
    ]
    for text in checked:
        lowered = str(text or "").lower()
        if any(marker in lowered for marker in markers):
            raise ValueError("cluster label payload carries material boundary markers")
    return label


def build_label_prompt(item: InductionInput) -> list[dict[str, str]]:
    """Prompt messages for naming one aggregated pattern."""
    shape_parts: list[str] = []
    for shape in item.arg_shapes[:6]:
        api = str(shape.get("api") or "").strip()
        args = shape.get("args", [])
        count = shape.get("count", 0)
        if api:
            shape_parts.append(f"{api}({args}) x{count}")
        else:
            shape_parts.append(f"{args} x{count}")
    shapes = "; ".join(shape_parts)
    siblings = ", ".join(item.siblings[:6])
    allowed = sorted(set(item.pattern) | {s for s in item.siblings if s})
    scope_segment = f"Scope: {item.scope.strip()}. " if item.scope.strip() else ""
    sig_parts: list[str] = []
    for api in item.pattern:
        raw_sig = (item.signatures.get(api) or "").strip()
        if not raw_sig:
            continue
        cleaned = raw_sig.removeprefix("def ").strip()
        cleaned = cleaned.replace("def ", "")
        if cleaned:
            sig_parts.append(f"{api}: {cleaned}")
    sigs = "; ".join(sig_parts[:6])
    sig_segment = f"Signatures: {sigs}. " if sigs else ""
    source_segment = (
        f"Source context: {item.source_hint.strip()}. "
        "Treat provenance as evidence, not as an official recommendation. "
        if item.source_hint.strip()
        else ""
    )
    user = (
        "Name and describe this API usage pattern. "
        f"{scope_segment}{source_segment}"
        f"Pattern: {' -> '.join(item.pattern)}. "
        f"Support: {item.support}. "
        f"Argument shapes: {shapes or 'unknown'}. "
        f"{sig_segment}"
        f"Co-used APIs: {siblings or 'none'}. "
        f"Allowed related APIs: {', '.join(allowed) if allowed else 'none'}. "
        "Reply with JSON only: "
        '{"name": "...", "summary": "...", '
        '"category_path": ["...", "..."], '
        '"workflow": "...", "related_apis": ["..."]}. '
        "Derive category words from the API dotted names, coarse to fine, at most 4 levels. "
        "Related APIs must be chosen from the allowed APIs only."
    )
    return [
        {
            "role": "system",
            "content": "You label mined API patterns with short stable names. Reply with JSON only.",
        },
        {"role": "user", "content": user},
    ]


def parse_label_payload(raw: str, allowed_apis: set[str]) -> PatternLabel:
    """Parse and validate one model reply; rejects unknown API references."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"label payload is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError("label payload must be an object")
    label = PatternLabel.model_validate(data)
    unknown = [a for a in label.related_apis if a not in allowed_apis]
    if unknown:
        raise ValueError(f"related APIs are outside the version: {unknown}")
    markers = (
        "<unit",
        "</unit",
        "<materials",
        "</materials",
        "<excerpt",
        "<hit",
        "<call",
        "<gap",
        "<reference",
        "<omitted",
    )
    checked = [
        label.name,
        label.summary,
        label.workflow,
        *label.category_path,
        *label.related_apis,
    ]
    for text in checked:
        lowered = str(text or "").lower()
        if any(marker in lowered for marker in markers):
            raise ValueError("label payload carries material boundary markers")
    return label
