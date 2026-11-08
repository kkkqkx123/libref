"""声明式语言配置：新增语言只加查询模块并登记。"""

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
    config_version: int = 4


PYTHON_CONFIG = LanguageConfig(
    name="python",
    grammar="tree_sitter_python",
    file_suffixes=(".py",),
    config_version=4,
)

JAVA_CONFIG = LanguageConfig(
    name="java",
    grammar="tree_sitter_java",
    file_suffixes=(".java",),
    config_version=4,
)

JAVASCRIPT_CONFIG = LanguageConfig(
    name="javascript",
    grammar="tree_sitter_javascript",
    file_suffixes=(".js", ".mjs", ".cjs"),
    config_version=4,
)

LANGUAGE_CONFIGS: dict[str, LanguageConfig] = {
    "python": PYTHON_CONFIG,
    "java": JAVA_CONFIG,
    "javascript": JAVASCRIPT_CONFIG,
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
    return Language(mod.language())


def check_config(cfg: LanguageConfig) -> None:
    if not cfg.file_suffixes or not all(s.startswith(".") for s in cfg.file_suffixes):
        raise ValueError(f"bad suffixes for {cfg.name}")
    language = load_language(cfg)
    from tree_sitter import Query

    for kind in _queries.KINDS:
        Query(language, _queries.get(cfg.name, kind))
