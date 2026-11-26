"""Public export facts bridge documentation names to internal declarations."""

from __future__ import annotations

import pytest

from libref.astminer.code_blocks import import_context_from_text
from libref.astminer.exports import ExportFact, export_facts

pytestmark = pytest.mark.unit


def test_python_star_and_all_exports() -> None:
    facts = export_facts(
        "httpx/__init__.py",
        "python",
        b'from ._api import *\n__all__ = ["get", "Client"]\n',
    )
    assert ExportFact("httpx.*", "httpx._api", "python_star") in facts
    assert ExportFact("httpx.get", "httpx.get", "python_all") in facts


def test_typescript_wildcard_namespace_and_named_exports() -> None:
    facts = export_facts(
        "packages/zod/src/v4/classic/external.ts",
        "typescript",
        b'export * from "./schemas.js";\n'
        b'export * as core from "../core/index.js";\n'
        b'export { toJSONSchema } from "../core/index.js";\n',
    )
    assert (
        ExportFact(
            "packages.zod.src.v4.classic.external.*",
            "packages.zod.src.v4.classic.schemas",
            "js_star",
        )
        in facts
    )
    assert (
        ExportFact(
            "packages.zod.src.v4.classic.external.core.*",
            "packages.zod.src.v4.core.index",
            "js_namespace",
        )
        in facts
    )
    assert (
        ExportFact(
            "packages.zod.src.v4.classic.external.toJSONSchema",
            "packages.zod.src.v4.core.index.toJSONSchema",
            "js_named",
        )
        in facts
    )


def test_rust_pub_use_exports() -> None:
    facts = export_facts(
        "core/lib/src/lib.rs",
        "rust",
        b"pub use rkt::Rocket;\npub use config::{Config, ConfigError as Error};\n",
    )
    assert (
        ExportFact("core.lib.src.Rocket", "core.lib.src.rkt.Rocket", "rust_pub_use")
        in facts
    )
    assert (
        ExportFact(
            "core.lib.src.Error", "core.lib.src.config.ConfigError", "rust_pub_use"
        )
        in facts
    )


def test_interactive_import_lines_become_context() -> None:
    text = ">>> import httpx\n>>> r = httpx.get(1)\n$ pip install httpx\n"
    assert import_context_from_text(text) == "import httpx"
