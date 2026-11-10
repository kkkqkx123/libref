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
    "TaxonomyNode",
    "build_label_prompt",
    "build_taxonomy_prompt",
    "merge_taxonomy",
    "parse_label_payload",
    "pattern_hash",
]

SKIP_FIELDS = frozenset({"summary", "purpose", "issues & bugs", "potential scenario"})


class InductionInput(BaseModel):
    pattern: list[str] = Field(min_length=1)
    support: int = Field(ge=1)
    arg_shapes: list[dict[str, Any]] = Field(default_factory=list)
    representative: str = Field(default="")
    siblings: list[str] = Field(default_factory=list)


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


class TaxonomyNode(BaseModel):
    path: str = Field(min_length=1)
    children: list[str] = Field(default_factory=list)


def pattern_hash(pattern: list[str]) -> str:
    blob = json.dumps(list(pattern), ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha1(blob).hexdigest()


def build_label_prompt(item: InductionInput) -> list[dict[str, str]]:
    """Prompt messages for naming one aggregated pattern."""
    shapes = "; ".join(
        f"{s.get('args', [])} x{s.get('count', 0)}" for s in item.arg_shapes[:3]
    )
    siblings = ", ".join(item.siblings[:6])
    user = (
        "Name and describe this API usage pattern. "
        f"Pattern: {' -> '.join(item.pattern)}. "
        f"Support: {item.support}. "
        f"Argument shapes: {shapes or 'unknown'}. "
        f"Co-used APIs: {siblings or 'none'}. "
        "Reply with JSON only: "
        '{"name": "...", "summary": "...", '
        '"category_path": ["...", "..."], '
        '"workflow": "...", "related_apis": ["..."]}. '
        "Category path must be coarse to fine with at most 4 levels. "
        "Related APIs must be chosen from the pattern and co-used APIs only."
    )
    return [
        {
            "role": "system",
            "content": "You label mined API patterns with short stable names. Reply with JSON only.",
        },
        {"role": "user", "content": user},
    ]


def build_taxonomy_prompt(names: list[str]) -> list[dict[str, str]]:
    """Prompt messages for grouping sampled API names into a nested tree."""
    sampled = "\n".join(f"- {name}" for name in names)
    return [
        {
            "role": "system",
            "content": "You group API names into a nested category tree. Reply with JSON only.",
        },
        {
            "role": "user",
            "content": (
                "Group these APIs into nested categories. "
                "Depth is arbitrary but at most 4. "
                f"APIs:\n{sampled}\n"
                'Reply with JSON only like {"Category": {"Sub": ["a.b", "a.c"]}}.'
            ),
        },
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
    return label


def merge_taxonomy(trees: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic merge of sampled trees keyed by category path."""
    merged: dict[str, Any] = {}

    def _insert(node: dict[str, Any], path: tuple[str, ...], leaves: list[str]) -> None:
        if not path:
            return
        head = path[0]
        slot = node.setdefault(head, {})
        if len(path) == 1:
            existing = slot.setdefault("__apis__", [])
            for leaf in sorted(set(leaves)):
                if leaf not in existing:
                    existing.append(leaf)
            existing.sort()
            return
        _insert(slot, path[1:], leaves)

    def _walk(node: Any, prefix: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key in sorted(node):
                if key == "__apis__":
                    continue
                child = node[key]
                if isinstance(child, dict):
                    _walk(child, (*prefix, str(key)))
                elif isinstance(child, list):
                    _insert(
                        merged, (*prefix, str(key)), [str(v) for v in child if str(v)]
                    )
        elif isinstance(node, list):
            _insert(merged, prefix, [str(v) for v in node if str(v)])

    for tree in trees:
        if isinstance(tree, dict):
            _walk(tree, ())
    return merged
