"""Documentation code blocks carry import context for alias resolution."""

from __future__ import annotations

import pytest

from libref.astminer.code_blocks import fenced_blocks, import_context_from_text

pytestmark = pytest.mark.unit


def test_import_context_collects_import_like_lines() -> None:
    text = "Intro\nimport org.example.Client;\nfrom demo import Client\nBody\n"
    assert import_context_from_text(text).splitlines() == [
        "import org.example.Client;",
        "from demo import Client",
    ]


def test_fenced_block_keeps_context_without_changing_code() -> None:
    text = 'import "example.com/demo"\n\n```go\ndemo.Client.Connect()\n```\n'
    blocks = fenced_blocks(text, sample_prefix="doc")
    assert len(blocks) == 1
    assert blocks[0].code == "demo.Client.Connect()"
    assert blocks[0].import_context == 'import "example.com/demo"'
