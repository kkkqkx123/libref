"""Gradle dependency edges: build-script declaration calls plus version-catalog join.

Purpose-built parsing over build scripts with the Groovy/Kotlin grammars; the
generic code index still owns entities and calls. Only Maven-coordinate
dependencies are returned: plugin declarations target the plugin portal, local
project and file dependencies carry no registry identity, and both are skipped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from libref.astminer import config as _config
from libref.astminer.manifests import ManifestDep

_BUILD_PATTERNS = ("build.gradle", "build.gradle.kts")
_CATALOG_CANDIDATES = ("gradle/libs.versions.toml", "libs.versions.toml")

_DEP_SUFFIXES = ("implementation", "compileonly", "runtimeonly", "api")


def _is_dep_config(name: str) -> bool:
    normalized = name.strip().lower()
    return normalized == "api" or normalized.endswith(_DEP_SUFFIXES)


def _unquote(text: str) -> str | None:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return None


def _strip_parens(text: str) -> str:
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        return text[1:-1].strip()
    return text


def _split_coordinate(text: str) -> tuple[str, str] | None:
    parts = text.strip().split(":")
    if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return (
        f"{parts[0].strip()}:{parts[1].strip()}",
        parts[2].strip() if len(parts) > 2 else "",
    )


def _unwrap_platform(text: str) -> str:
    lowered = text.strip().lower()
    for wrapper in ("platform(", "enforcedplatform("):
        if lowered.startswith(wrapper) and text.strip().endswith(")"):
            return _strip_parens(text.strip()[len(wrapper) - 1 :])
    return text


def _catalog_key(parts: tuple[str, ...]) -> tuple[str, ...]:
    import re

    out: list[str] = []
    for part in parts:
        out.extend(p for p in re.split(r"[-_.]", part) if p)
    return tuple(out)


def _load_catalog(root: Path) -> dict[tuple[str, ...], tuple[str, str]]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.11+ always ships tomllib
        return {}
    for candidate in _CATALOG_CANDIDATES:
        try:
            text = (root / candidate).read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            data = tomllib.loads(text)
        except ValueError:
            return {}
        return _parse_catalog(data)
    return {}


def _catalog_version(entry: object, versions: dict) -> str:
    if isinstance(entry, str):
        ref = entry.strip()
        return _catalog_version(versions.get(ref), versions) if ref in versions else ref
    if not isinstance(entry, dict):
        return ""
    for key in ("version", "require", "strictly", "prefer"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    ref_name = entry.get("ref")
    if isinstance(ref_name, str) and ref_name.strip():
        return _catalog_version(versions.get(ref_name.strip()), versions)
    return ""


def _parse_catalog(data) -> dict[tuple[str, ...], tuple[str, str]]:
    if not isinstance(data, dict):
        return {}
    libraries = data.get("libraries")
    versions = data.get("versions")
    if not isinstance(libraries, dict):
        return {}
    if not isinstance(versions, dict):
        versions = {}
    out: dict[tuple[str, ...], tuple[str, str]] = {}
    for alias, entry in libraries.items():
        module, version = "", ""
        if isinstance(entry, str):
            coordinate = _split_coordinate(entry)
            if coordinate is None:
                continue
            module, version = coordinate
        elif isinstance(entry, dict):
            raw_module = entry.get("module")
            if isinstance(raw_module, str):
                coordinate = _split_coordinate(raw_module)
                if coordinate is not None:
                    module = coordinate[0]
            version = _catalog_version(entry.get("version"), versions)
        if module:
            out[_catalog_key((str(alias),))] = (module, version)
    return out


def _resolve_arg(arg_text: str, catalog: dict) -> tuple[str, str] | None:
    inner = _strip_parens(arg_text)
    inner = _unwrap_platform(inner)
    literal = _unquote(inner)
    if literal is not None:
        return _split_coordinate(literal)
    if inner.startswith("libs."):
        key = _catalog_key(tuple(inner[len("libs.") :].split(".")))
        hit = catalog.get(key)
        if hit is None:
            lowered = tuple(p.lower() for p in key)
            for stored, value in catalog.items():
                if tuple(p.lower() for p in stored) == lowered:
                    hit = value
                    break
        return hit
    return None


def _dep_queries():
    from tree_sitter import Query

    groovy = _config.load_language(_config.GROOVY_CONFIG)
    kotlin = _config.load_language(_config.KOTLIN_CONFIG)
    groovy_query = Query(
        groovy,
        r"""
(juxt_function_call
  [(identifier) (dotted_identifier)] @dep.config
  (argument_list) @dep.args) @dep.call
(function_call
  function: [(identifier) (dotted_identifier)] @dep.config
  (argument_list) @dep.args) @dep.call
""",
    )
    kotlin_query = Query(
        kotlin,
        r"""
(call_expression
  [(simple_identifier) (navigation_expression)] @dep.config
  (call_suffix)? @dep.args) @dep.call
""",
    )
    return ((groovy, groovy_query), (kotlin, kotlin_query))


def _extract_calls(source: bytes, language, query) -> list[tuple[str, str, Any]]:
    from tree_sitter import Parser, QueryCursor

    from libref.astminer.rules import slice_text

    tree = Parser(language).parse(source)
    out: list[tuple[str, str, Any]] = []
    for pattern, captures in QueryCursor(query).matches(tree.root_node):
        configs = captures.get("dep.config", [])
        args = captures.get("dep.args", [])
        calls = captures.get("dep.call", [])
        if not configs or not args or not calls:
            continue
        name = slice_text(configs[0], source).strip()
        out.append((name, slice_text(args[0], source), calls[0]))
    return out


def _platform_follow_up(call_node, source: bytes) -> str | None:
    """Recover the version string when a juxtaposed platform wrapper splits across siblings."""
    from libref.astminer.rules import slice_text

    sibling = call_node.next_named_sibling
    if sibling is None:
        return None
    if (
        sibling.type == "parenthesized_expression"
        or sibling.type == "expression_statement"
    ):
        candidates = [sibling]
    else:
        return None
    stack = list(candidates)
    while stack:
        node = stack.pop()
        if node.type == "parenthesized_expression":
            for child in node.children:
                if child.is_named and _unquote(slice_text(child, source)) is not None:
                    return slice_text(child, source)
            return None
        stack.extend(node.children)
    return None


def _build_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for pattern in _BUILD_PATTERNS:
        found.extend(sorted(root.rglob(pattern)))
    return found


def gradle_dependencies(root) -> list[ManifestDep]:
    """Maven-coordinate edges from Gradle build scripts; unparsable files yield nothing."""
    from libref.astminer.manifests import normalize_dep_name

    base = Path(root)
    catalog = _load_catalog(base)
    queries = _dep_queries()
    out: list[ManifestDep] = []
    seen: set[tuple[str, str]] = set()

    def _push(name: str, req: str, source_file: str) -> None:
        norm = normalize_dep_name("maven", name)
        if norm and (norm, source_file) not in seen:
            seen.add((norm, source_file))
            out.append(
                ManifestDep(
                    ecosystem="maven", name=norm, req=req, source_file=source_file
                )
            )

    for path in _build_files(base):
        is_kotlin = path.suffix == ".kts"
        language, query = queries[1] if is_kotlin else queries[0]
        try:
            source = path.read_bytes()
        except OSError:
            continue
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:
            rel = path.name
        for config_name, arg_text, call_node in _extract_calls(source, language, query):
            if not _is_dep_config(config_name):
                continue
            resolved = _resolve_arg(arg_text, catalog)
            if resolved is None and _strip_parens(arg_text).strip().lower() in (
                "platform",
                "enforcedplatform",
            ):
                follow_up = _platform_follow_up(call_node, source)
                if follow_up is not None:
                    resolved = _resolve_arg(follow_up, catalog)
            if resolved is None:
                continue
            _push(resolved[0], resolved[1], rel)
    return out
