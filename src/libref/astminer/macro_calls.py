"""Call-shaped evidence found in macro bodies without preprocessing expansion."""

from __future__ import annotations

import hashlib
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
_FRAGMENT_PREFIX = b"void __libref_macro_probe(void) { "
_FRAGMENT_SUFFIX = b" }"
_fragment_cache: dict[str, list[tuple[str, int, int, list[str]]]] = {}


@dataclass(frozen=True)
class MacroCall:
    """One call visible in a macro body, attributed to its defining macro."""

    name: str
    start_byte: int
    end_byte: int
    line_start: int
    line_end: int
    args: list[str] = field(default_factory=list)
    host_macro: str = ""
    host_file: str = ""
    macro_definition_line: int = 0
    macro_body_kind: str = ""
    macro_body_signature: str = ""
    macro_body_hash: str = ""
    macro_nesting: int = 0
    macro_conditions: list[str] = field(default_factory=list)


def macro_body_signature(text: str) -> str:
    """Stable, shortened text used to describe one macro body."""
    return " ".join(str(text or "").split())[:240]


def macro_body_hash(text: str) -> str:
    """Identity of the exact macro definition text, independent of file offsets."""
    blob = macro_body_signature(text).encode()
    return hashlib.sha1(blob).hexdigest()


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _preproc_conditions(node, source: bytes) -> list[str]:
    """Record the conditional text surrounding one macro definition."""
    out: list[str] = []
    parent = node.parent
    while parent is not None:
        if parent.type.startswith("preproc_if"):
            text = " ".join(slice_text(parent, source).splitlines()[0].split())
            if text and text not in out:
                out.append(text)
        parent = parent.parent
    return out


def _line_span(node, text: str, start: int, end: int) -> tuple[int, int]:
    first = node.start_point[0] + 1
    return first + text[:start].count("\n"), first + text[:end].count("\n")


def _paren_depth(text: str) -> int:
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
    return depth


def _arguments_from_text(text: str, open_index: int) -> list[str]:
    """Split one argument list by top-level commas; no preprocessor expansion."""
    depth = 0
    start = open_index + 1
    args: list[str] = []
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                args.append(text[start:index].strip())
                break
            depth -= 1
        elif char == "," and depth == 0:
            args.append(text[start:index].strip())
            start = index + 1
    return [arg for arg in args if arg][:16]


def _parsed_body_calls(text: str, parser) -> list[tuple[str, int, int, list[str]]]:
    """Parse a macro body as a function body and keep real call expressions."""
    cached = _fragment_cache.get(text)
    if cached is not None:
        return cached
    if parser is None:
        return []
    from libref.astminer.rules import summarize_arguments

    fragment = _FRAGMENT_PREFIX + text.encode() + _FRAGMENT_SUFFIX
    root = parser.parse(fragment).root_node
    found: list[tuple[str, int, int, list[str]]] = []
    offset = len(_FRAGMENT_PREFIX)
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        function = node.child_by_field_name("function")
        arguments = node.child_by_field_name("arguments")
        if function is None:
            continue
        name = slice_text(function, fragment).strip()
        if not name or name in _NON_CALLS or not name[0].isalpha() and name[0] != "_":
            continue
        args = (
            summarize_arguments(arguments, fragment)
            if arguments is not None and arguments.type == "argument_list"
            else []
        )
        found.append(
            (name, function.start_byte - offset, node.end_byte - offset, list(args))
        )
    found.sort(key=lambda item: (item[1], item[2]))
    _fragment_cache[text] = found
    return found


def _macro_context(node, source: bytes, path: str, body_kind: str) -> dict:
    name_node = node.child_by_field_name("name")
    host = slice_text(name_node, source).strip() if name_node is not None else ""
    definition = slice_text(node, source)
    return {
        "host_macro": host,
        "host_file": path,
        "macro_definition_line": node.start_point[0] + 1,
        "macro_body_kind": body_kind,
        "macro_body_signature": macro_body_signature(definition),
        "macro_body_hash": macro_body_hash(definition),
        "macro_conditions": _preproc_conditions(node, source),
    }


def _c_macro_body_calls(node, source: bytes, path: str, parser) -> list[MacroCall]:
    """Scan macro value text, preferring parsed fragments over token matching."""
    value = node.child_by_field_name("value")
    if value is None or value.type != "preproc_arg":
        return []
    text = slice_text(value, source)
    if not _MACRO_CALL_RE.search(text):
        return []
    body_kind = (
        "function_macro" if node.type == "preproc_function_def" else "object_macro"
    )
    context = _macro_context(node, source, path, body_kind)
    out: list[MacroCall] = []
    parsed = _parsed_body_calls(text, parser)
    for name, name_offset, end_offset, args in parsed:
        start = value.start_byte + name_offset
        line_start, line_end = _line_span(value, text, name_offset, end_offset)
        out.append(
            MacroCall(
                name=name.rpartition(".")[2],
                start_byte=start,
                end_byte=value.start_byte + end_offset,
                line_start=line_start,
                line_end=line_end,
                args=args,
                macro_nesting=_paren_depth(text[:name_offset]),
                **context,
            )
        )
    parsed_starts = {item[1] for item in parsed}
    for match in _MACRO_CALL_RE.finditer(text):
        name = match.group(1)
        if name in _NON_CALLS or match.start(1) in parsed_starts:
            continue
        line_start, line_end = _line_span(value, text, match.start(), match.end())
        out.append(
            MacroCall(
                name=name,
                start_byte=value.start_byte + match.start(),
                end_byte=value.start_byte + match.end(),
                line_start=line_start,
                line_end=line_end,
                args=_arguments_from_text(text, match.end() - 1),
                macro_nesting=_paren_depth(text[: match.start()]),
                **context,
            )
        )
    return out


def _rust_token_calls(node, source: bytes, path: str) -> list[MacroCall]:
    """Rust token trees hold `name(...)` as adjacent identifier + token_tree."""
    out: list[MacroCall] = []
    context = _macro_context(node, source, path, "rust_macro")
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
                    macro_nesting=_paren_depth(slice_text(current, source)),
                    **context,
                )
            )
    return out


def find_macro_calls(
    root, source: bytes, language: str, path: str = "", parser=None
) -> list[MacroCall]:
    """Find macro-contained call tokens without attempting macro expansion."""
    if language == "rust":
        hosts = ("macro_invocation", "macro_definition")
        scan = lambda node: _rust_token_calls(node, source, path)
    elif language in ("c", "cpp"):
        hosts = ("preproc_function_def", "preproc_def")
        scan = lambda node: _c_macro_body_calls(node, source, path, parser)
    else:
        return []
    out: list[MacroCall] = []
    for current in _walk(root):
        if current.type in hosts:
            out.extend(scan(current))
    out.sort(key=lambda call: (call.start_byte, call.end_byte))
    return out
