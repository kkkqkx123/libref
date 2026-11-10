"""Text shaping leaf: normalization, query, snippet, embedding text.

Pure functions with no database or network access. All upper layers
(crawler, chunking, store, services) share this leaf instead of
duplicating string transforms.
"""

from libref.text.bm25 import clean_text_for_bm25
from libref.text.cjk import CJK_RUN_RE, expand_cjk_run
from libref.text.embedding import (
    build_embed_text,
    is_embeddable_metadata,
    strip_markdown_for_embedding,
)
from libref.text.ident import ident_snake_form, snake_variant
from libref.text.normalize import (
    classify_content,
    normalize_markdown,
    wrap_source_content,
)
from libref.text.query import safe_fts_query
from libref.text.snippet import (
    call_tag,
    gap_tag,
    make_snippet,
    omitted_tag,
    reference_tag,
    truncate_material,
    unit_tag,
    xml_escape_attr,
)

__all__ = [
    "CJK_RUN_RE",
    "build_embed_text",
    "call_tag",
    "classify_content",
    "clean_text_for_bm25",
    "expand_cjk_run",
    "gap_tag",
    "ident_snake_form",
    "is_embeddable_metadata",
    "make_snippet",
    "normalize_markdown",
    "omitted_tag",
    "reference_tag",
    "safe_fts_query",
    "snake_variant",
    "strip_markdown_for_embedding",
    "truncate_material",
    "unit_tag",
    "wrap_source_content",
    "xml_escape_attr",
]
