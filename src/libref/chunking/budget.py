"""Chunk budgets and metering: dual-path conventions, hard limits and planning reserves, plus term counting matching the index.

The embedding path estimates tokens by character class: letters, symbols, whitespace, CJK and
other scripts carry different per-character costs in the embedding model's tokenizer, so one
flat ratio would make the same budget mean different real sizes for prose, code and Chinese.
The weights are calibrated against measured token usage and rounded up, so a budget is a cap
rather than an aspiration; recalibrate them whenever the embedding model changes.

Term counting is a separate contract: it mirrors the full-text index's tokenization rules
(Latin identifiers count as one, CJK counts per character), and the index expands CJK per
character, so the planning convention holds for the full-text path.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import Enum

_LATIN_RUN_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")

# Per-character token weights for the embedding convention, calibrated on measured usage of
# the configured embedding model and rounded up. Letters are ASCII alphanumerics, symbols are
# ASCII punctuation, CJK covers ideographs plus kana and hangul, and anything else counts whole.
_LETTER_WEIGHT = 0.25
_SYMBOL_WEIGHT = 1.0
_SPACE_WEIGHT = 0.2
_CJK_WEIGHT = 1.0
_OTHER_WEIGHT = 1.0

_ASCII_LETTER_RE = re.compile(r"[A-Za-z0-9_]")
_ASCII_SPACE_RE = re.compile(r"[ \t\n\r\f\v]")
_KANA_HANGUL_RE = re.compile(r"[\u3040-\u30ff\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]")


class ChunkPath(str, Enum):
    BM25 = "bm25"
    EMBEDDING = "embedding"


@dataclass(frozen=True)
class ChunkBudget:
    hard_limit: int
    min_limit: int
    overlap: int
    reserve_ratio: float = 0.06

    @property
    def body_limit(self) -> int:
        """Assembly limit for splitting, reserving room for downstream decoration without touching the hard limit."""
        if self.hard_limit <= 0:
            return 0
        reserve = int(self.hard_limit * self.reserve_ratio)
        return max(1, self.hard_limit - reserve)


def budgets_from_settings(settings) -> dict[ChunkPath, ChunkBudget]:
    """Build dual-path budgets from settings; the sole entry point for chunking and preview."""
    reserve = float(settings.chunk_reserve_ratio)
    return {
        ChunkPath.EMBEDDING: ChunkBudget(
            hard_limit=int(settings.chunk_embed_max_tokens),
            min_limit=int(settings.chunk_embed_min_tokens),
            overlap=int(settings.chunk_embed_overlap_tokens),
            reserve_ratio=reserve,
        ),
        ChunkPath.BM25: ChunkBudget(
            hard_limit=int(settings.chunk_bm25_max_terms),
            min_limit=int(settings.chunk_bm25_min_terms),
            overlap=int(settings.chunk_bm25_overlap_terms),
            reserve_ratio=reserve,
        ),
    }


def default_budgets(settings=None) -> dict[ChunkPath, ChunkBudget]:
    """Default budgets; without settings, resolved from the settings class defaults rather than copied values."""
    if settings is not None:
        return budgets_from_settings(settings)
    from libref.config import LibrefSettings

    fields = LibrefSettings.model_fields
    reserve = float(fields["chunk_reserve_ratio"].default)
    return {
        ChunkPath.EMBEDDING: ChunkBudget(
            hard_limit=int(fields["chunk_embed_max_tokens"].default),
            min_limit=int(fields["chunk_embed_min_tokens"].default),
            overlap=int(fields["chunk_embed_overlap_tokens"].default),
            reserve_ratio=reserve,
        ),
        ChunkPath.BM25: ChunkBudget(
            hard_limit=int(fields["chunk_bm25_max_terms"].default),
            min_limit=int(fields["chunk_bm25_min_terms"].default),
            overlap=int(fields["chunk_bm25_overlap_terms"].default),
            reserve_ratio=reserve,
        ),
    }


EXACT_ENTITIES = "exact_entities"
ENCLOSING_ENTITY = "enclosing_entity"
GROUP_FALLBACK = "group_fallback"
DOCUMENT_RANGE = "document_range"
SPAN_UNAVAILABLE = "unavailable"


def estimate_tokens(text: str) -> int:
    """Token estimate in the embedding convention, charged per character class and rounded up."""
    if not text.strip():
        return 0
    if text.isascii():
        letters = len(_ASCII_LETTER_RE.findall(text))
        spaces = len(_ASCII_SPACE_RE.findall(text))
        symbols = len(text) - letters - spaces
        total = (
            letters * _LETTER_WEIGHT + spaces * _SPACE_WEIGHT + symbols * _SYMBOL_WEIGHT
        )
        return math.ceil(total)
    total = 0.0
    for ch in text:
        if ch.isascii():
            if "0" <= ch <= "9" or "a" <= ch <= "z" or "A" <= ch <= "Z" or ch == "_":
                total += _LETTER_WEIGHT
            elif ch in " \t\n\r\f\v":
                total += _SPACE_WEIGHT
            else:
                total += _SYMBOL_WEIGHT
        elif _CJK_RE.match(ch) or _KANA_HANGUL_RE.match(ch):
            total += _CJK_WEIGHT
        else:
            total += _OTHER_WEIGHT
    return math.ceil(total)


def count_terms(text: str) -> int:
    """Term count: Latin identifiers count as one, CJK counts per character; the latter is deliberately conservative."""
    if not text.strip():
        return 0
    latin = len(_LATIN_RUN_RE.findall(text))
    cjk = len(_CJK_RE.findall(text))
    return latin + cjk


def cost(text: str, path: ChunkPath) -> int:
    if path == ChunkPath.BM25:
        return count_terms(text)
    return estimate_tokens(text)


def fits(text: str, path: ChunkPath, limit: int) -> bool:
    return limit == 0 or cost(text, path) <= limit


def truncate_to_cost(text: str, path: ChunkPath, limit: int) -> str:
    """Take the longest valid prefix at a character boundary; the caller is responsible for over-limit warnings."""
    if limit == 0 or cost(text, path) <= limit:
        return text
    chars = list(text)
    lo, hi = 0, len(chars)
    while lo < hi:
        mid = lo + (hi - lo + 1) // 2
        if cost("".join(chars[:mid]), path) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return "".join(chars[:lo])


def split_lines_by_cost(text: str, path: ChunkPath, limit: int) -> list[str]:
    """Sole fallback for greedy line-based assembly; oversized single lines are hard-split by character budget."""
    if not text:
        return []
    if limit == 0:
        return [text]
    parts: list[str] = []
    cur: list[str] = []
    cur_cost = 0
    for line in text.splitlines():
        line_cost = cost(line, path) + 1
        if cur and cur_cost + line_cost > limit:
            parts.append("\n".join(cur))
            cur, cur_cost = [], 0
        if not cur and line_cost > limit:
            head = truncate_to_cost(line, path, limit)
            parts.append(head if head else line[:1])
            rest = line[len(head) :] if head else line[1:]
            while rest:
                head = truncate_to_cost(rest, path, limit)
                parts.append(head if head else rest[:1])
                rest = rest[len(head) :] if head else rest[1:]
            continue
        cur.append(line)
        cur_cost += line_cost
    if cur:
        parts.append("\n".join(cur))
    return parts
