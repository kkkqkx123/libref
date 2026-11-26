"""Language evidence for documentation snippets is resolved by explicit priority."""

from __future__ import annotations

import pytest

from libref.astminer.code_blocks import fenced_blocks
from libref.astminer.language_evidence import (
    LanguageFacts,
    canonical_language,
    resolve_language,
)
from libref.crawler.code_blocks import html_code_blocks

pytestmark = pytest.mark.unit


def test_block_tag_wins_over_context_and_rules() -> None:
    decision = resolve_language(
        LanguageFacts(
            block_tag="py",
            ancestor_tag="highlight-java",
            nearby_text="Python example",
            manual_rules={"url:.*": "go"},
        )
    )
    assert (decision.language, decision.source) == ("python", "block_tag")


def test_ancestor_class_is_language_evidence() -> None:
    html = '<div class="highlight-csharp"><pre><code>RuleFor(x =&gt; x.Id);</code></pre></div>'
    blocks = html_code_blocks(html, sample_prefix="doc")
    assert len(blocks) == 1
    assert blocks[0].language == "csharp"
    assert blocks[0].language_source == "ancestor_class"


def test_nearby_heading_is_language_evidence() -> None:
    text = "## Kotlin example\n\n```\nval client = Client()\n```\n"
    blocks = fenced_blocks(text, sample_prefix="doc")
    assert len(blocks) == 1
    assert blocks[0].language == "kotlin"
    assert blocks[0].language_source == "nearby_heading"


def test_manual_rule_records_its_identifier() -> None:
    decision = resolve_language(
        LanguageFacts(
            url="https://example.test/api",
            selector="div.highlight pre",
            manual_rules={"selector:highlight": "groovy"},
        )
    )
    assert (decision.language, decision.source, decision.rule) == (
        "groovy",
        "manual_rule",
        "selector:highlight",
    )


def test_candidates_stay_separate_for_parse_validation() -> None:
    decision = resolve_language(LanguageFacts(candidates=("python", "shell", "python")))
    assert decision.language == ""
    assert decision.candidates == ("python",)


def test_unsupported_tag_and_no_evidence_are_distinct() -> None:
    assert canonical_language("shell") == ""
    unsupported = fenced_blocks(
        "```bash\n$ pip install demo\n```\n", sample_prefix="doc"
    )
    unlabeled = fenced_blocks("```\nplain text\n```\n", sample_prefix="doc")
    assert unsupported[0].raw_language == "shell"
    assert unsupported[0].language == ""
    assert unlabeled[0].raw_language == ""
    assert unlabeled[0].language_source == ""
