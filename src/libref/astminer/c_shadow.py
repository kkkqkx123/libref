"""Byte-preserving shadow source for C/C++ interop guards fed to tree-sitter."""

from __future__ import annotations

_SHADOW_LANGUAGES = frozenset({"c", "cpp"})


def _line_text(line: bytes) -> str:
    return line.decode("ascii", "replace").rstrip("\r")


def _opens_on_cplusplus(line: bytes) -> bool:
    text = _line_text(line).lstrip()
    if not text.startswith("#"):
        return False
    rest = text[1:].lstrip()
    if rest.startswith("ifdef"):
        return rest[5:].strip() == "__cplusplus"
    if rest.startswith("if"):
        expr = rest[2:].strip()
        if not expr.startswith("defined"):
            return False
        expr = expr[7:].lstrip()
        if expr.startswith("(") and expr.endswith(")"):
            expr = expr[1:-1]
        return expr.strip() == "__cplusplus"
    return False


def _is_extern_c_brace(line: bytes) -> bool:
    text = _line_text(line).strip()
    if not text.startswith("extern"):
        return False
    rest = text[6:].lstrip()
    if not (rest.startswith('"C"')):
        return False
    return rest[3:].strip() == "{"


def _is_lone_brace(line: bytes) -> bool:
    return _line_text(line).strip() == "}"


def _is_endif(line: bytes) -> bool:
    text = _line_text(line).lstrip()
    if not text.startswith("#"):
        return False
    rest = text[1:].lstrip()
    if not rest.startswith("endif"):
        return False
    return not rest[5:].strip()


def _is_opener(lines: list[bytes], index: int) -> bool:
    return (
        _opens_on_cplusplus(lines[index])
        and _is_extern_c_brace(lines[index + 1])
        and _is_endif(lines[index + 2])
    )


def _is_closer(lines: list[bytes], index: int) -> bool:
    return (
        _opens_on_cplusplus(lines[index])
        and _is_lone_brace(lines[index + 1])
        and _is_endif(lines[index + 2])
    )


def _guard_lines(lines: list[bytes]) -> list[int]:
    """Line indices of the brace lines that form complete guard pairs."""
    out: list[int] = []
    start = 0
    while start + 3 <= len(lines):
        open_at = next(
            (i for i in range(start, len(lines) - 2) if _is_opener(lines, i)), None
        )
        if open_at is None:
            break
        close_at = next(
            (i for i in range(open_at + 3, len(lines) - 2) if _is_closer(lines, i)),
            None,
        )
        if close_at is None:
            break
        out.extend((open_at + 1, close_at + 1))
        start = close_at + 3
    return out


def shadow_for_parse(source: bytes, language: str) -> bytes:
    """Return parse-only source with paired ``extern "C"`` braces blanked.

    Only C and C++ are considered. Blanked lines keep their exact byte length,
    including CRLF, so all node spans still index into the original source.
    Unbalanced or unrecognized guards leave the source untouched.
    """
    if language not in _SHADOW_LANGUAGES or b"extern" not in source:
        return source
    lines = source.split(b"\n")
    targets = _guard_lines(lines)
    if not targets:
        return source
    shadow = list(lines)
    for index in targets:
        shadow[index] = b" " * len(shadow[index])
    return b"\n".join(shadow)
