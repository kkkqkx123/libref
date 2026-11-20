"""Low-confidence calls found inside macro definitions and macro arguments."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from libref.astminer.rules.text import slice_text

_MACRO_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_NON_CALLS = frozenset(
    {
        "if",
        "for",
        "while",
        "match",
        "loop",
        "return",
        "let",
        "mut",
        "pub",
        "crate",
        "use",
        "fn",
        "impl",
        "struct",
        "enum",
        "trait",
        "type",
        "const",
        "static",
        "macro_rules",
        "include",
        "concat",
        "stringify",
    }
)


@dataclass(frozen=True)
class MacroCall:
    """One call-shaped token found in a macro body or macro argument."""

    name: str
    start_byte: int
    end_byte: int
    line_start: int
    line_end: int
    args: list[str] = field(default_factory=list)


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _rust_token_calls(node, source: bytes) -> list[MacroCall]:
    """Rust token trees hold `name(...)` as adjacent identifier + token_tree."""
    out: list[MacroCall] = []
    for current in _walk(node):
        if current.type != "token_tree":
            continue
        named = [child for child in current.children if child.is_named]
        for index, child in enumerate(named[:-1]):
            following = named[index + 1]
            if child.type != "identifier" or following.type != "token_tree":
                continue
            name = slice_text(child, source).strip()
            if not name or name in _NON_CALLS:
                continue
            if not slice_text(following, source).lstrip().startswith("("):
                continue
            out.append(
                MacroCall(
                    name=name,
                    start_byte=child.start_byte,
                    end_byte=following.end_byte,
                    line_start=child.start_point[0] + 1,
                    line_end=following.end_point[0] + 1,
                )
            )
    return out


def _c_macro_body_calls(node, source: bytes) -> list[MacroCall]:
    """C function-like macro bodies are opaque text, so scan them as tokens."""
    value = node.child_by_field_name("value")
    if value is None or value.type != "preproc_arg":
        return []
    text = slice_text(value, source)
    out: list[MacroCall] = []
    for match in _MACRO_CALL_RE.finditer(text):
        name = match.group(1)
        if name in _NON_CALLS:
            continue
        out.append(
            MacroCall(
                name=name,
                start_byte=value.start_byte + match.start(),
                end_byte=value.start_byte + match.end(),
                line_start=value.start_point[0] + 1,
                line_end=value.end_point[0] + 1,
            )
        )
    return out


def find_macro_calls(root, source: bytes, language: str) -> list[MacroCall]:
    """Find macro-contained call tokens without attempting macro expansion."""
    hosts: tuple[str, ...]
    if language == "rust":
        hosts = ("macro_invocation", "macro_definition")
        scan = _rust_token_calls
    elif language in ("c", "cpp"):
        hosts = ("preproc_function_def",)
        scan = _c_macro_body_calls
    else:
        return []
    out: list[MacroCall] = []
    for current in _walk(root):
        if current.type in hosts:
            out.extend(scan(current, source))
    out.sort(key=lambda call: (call.start_byte, call.end_byte))
    return out
