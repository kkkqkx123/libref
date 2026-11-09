"""Text shaping leaf: normalization, query, snippet, embedding text.

Pure functions with no database or network access. All upper layers
(crawler, chunking, store, services) share this leaf instead of
duplicating string transforms.
"""

from libref.text.bm25 import clean_text_for_bm25
from libref.text.cjk import CJK_RUN_RE, expand_cjk_run
from libref.text.embedding import build_embed_text, strip_markdown_for_embedding
from libref.text.ident import ident_snake_form, snake_variant
from libref.text.normalize import (
    classify_content,
    normalize_markdown,
    wrap_source_content,
)
from libref.text.query import safe_fts_query
from libref.text.snippet import make_snippet, truncate_material

__all__ = [
    "build_embed_text",
    "classify_content",
    "CJK_RUN_RE",
    "clean_text_for_bm25",
    "expand_cjk_run",
    "ident_snake_form",
    "make_snippet",
    "normalize_markdown",
    "safe_fts_query",
    "snake_variant",
    "strip_markdown_for_embedding",
    "truncate_material",
    "wrap_source_content",
]
