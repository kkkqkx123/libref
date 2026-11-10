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
    "InductionInput",
    "PatternLabel",
    "build_label_prompt",
    "parse_label_payload",
    "pattern_hash",
]


class InductionInput(BaseModel):
    pattern: list[str] = Field(min_length=1)
    support: int = Field(ge=1)
    arg_shapes: list[dict[str, Any]] = Field(default_factory=list)
    siblings: list[str] = Field(default_factory=list)
    scope: str = Field(default="")


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
    user = (
        "Name and describe this API usage pattern. "
        f"{scope_segment}"
        f"Pattern: {' -> '.join(item.pattern)}. "
        f"Support: {item.support}. "
        f"Argument shapes: {shapes or 'unknown'}. "
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
