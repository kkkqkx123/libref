"""Declarative language config: adding a language only requires a query module and registration."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
from dataclasses import dataclass

from libref.astminer import queries as _queries


@dataclass(frozen=True)
class LanguageConfig:
    name: str
    grammar: str
    file_suffixes: tuple[str, ...]
    config_version: int = 1
    language_fn: str = "language"


PYTHON_CONFIG = LanguageConfig(
    name="python",
    grammar="tree_sitter_python",
    file_suffixes=(".py", ".pyi"),
    config_version=1,
)

JAVA_CONFIG = LanguageConfig(
    name="java",
    grammar="tree_sitter_java",
    file_suffixes=(".java",),
    config_version=1,
)

JAVASCRIPT_CONFIG = LanguageConfig(
    name="javascript",
    grammar="tree_sitter_javascript",
    file_suffixes=(".js", ".mjs", ".cjs", ".jsx"),
    config_version=1,
)

RUST_CONFIG = LanguageConfig(
    name="rust",
    grammar="tree_sitter_rust",
    file_suffixes=(".rs",),
    config_version=1,
)

GO_CONFIG = LanguageConfig(
    name="go",
    grammar="tree_sitter_go",
    file_suffixes=(".go",),
    config_version=1,
)

CSHARP_CONFIG = LanguageConfig(
    name="csharp",
    grammar="tree_sitter_c_sharp",
    file_suffixes=(".cs",),
    config_version=1,
)

KOTLIN_CONFIG = LanguageConfig(
    name="kotlin",
    grammar="tree_sitter_kotlin",
    file_suffixes=(".kt", ".kts"),
    config_version=1,
)

GROOVY_CONFIG = LanguageConfig(
    name="groovy",
    grammar="tree_sitter_groovy",
    file_suffixes=(".groovy",),
    config_version=1,
)

TYPESCRIPT_CONFIG = LanguageConfig(
    name="typescript",
    grammar="tree_sitter_typescript",
    file_suffixes=(".ts", ".mts", ".cts", ".tsx"),
    config_version=1,
    language_fn="language_tsx",
)

LANGUAGE_CONFIGS: dict[str, LanguageConfig] = {
    "python": PYTHON_CONFIG,
    "java": JAVA_CONFIG,
    "javascript": JAVASCRIPT_CONFIG,
    "typescript": TYPESCRIPT_CONFIG,
    "rust": RUST_CONFIG,
    "go": GO_CONFIG,
    "csharp": CSHARP_CONFIG,
    "kotlin": KOTLIN_CONFIG,
    "groovy": GROOVY_CONFIG,
}


def config_for_suffix(suffix: str) -> LanguageConfig | None:
    for cfg in LANGUAGE_CONFIGS.values():
        if suffix in cfg.file_suffixes:
            return cfg
    return None


def all_suffixes() -> tuple[str, ...]:
    seen: list[str] = []
    for cfg in LANGUAGE_CONFIGS.values():
        for suffix in cfg.file_suffixes:
            if suffix not in seen:
                seen.append(suffix)
    return tuple(seen)


def grammar_fingerprint(cfg: LanguageConfig) -> str:
    import tree_sitter

    runtime = getattr(tree_sitter, "__version__", "unknown")
    try:
        wheel = importlib.metadata.version(cfg.grammar)
    except importlib.metadata.PackageNotFoundError:
        wheel = "unknown"
    blob = "\x00".join(
        [
            runtime,
            wheel,
            cfg.grammar,
            getattr(cfg, "language_fn", "language"),
            cfg.name,
            str(cfg.config_version),
            _queries.fingerprint_fragment(cfg.name),
            ",".join(cfg.file_suffixes),
        ]
    )
    return hashlib.sha1(blob.encode()).hexdigest()


def load_language(cfg: LanguageConfig):
    from tree_sitter import Language

    mod = importlib.import_module(cfg.grammar)
    return Language(getattr(mod, getattr(cfg, "language_fn", "language"))())


def check_config(cfg: LanguageConfig) -> None:
    if not cfg.file_suffixes or not all(s.startswith(".") for s in cfg.file_suffixes):
        raise ValueError(f"bad suffixes for {cfg.name}")
    language = load_language(cfg)
    from tree_sitter import Query

    for kind in _queries.KINDS:
        Query(language, _queries.get(cfg.name, kind))
    _queries.validate_captures(cfg.name)
