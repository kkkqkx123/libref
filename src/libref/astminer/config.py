"""声明式语言配置：新增语言只加配置不改引擎。"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class LanguageConfig:
    name: str
    grammar: str
    class_types: tuple[str, ...]
    function_types: tuple[str, ...]
    import_types: tuple[str, ...]
    call_types: tuple[str, ...]
    call_function_field: str
    accessor_node_types: tuple[str, ...]
    receiver_types: tuple[str, ...]
    file_suffixes: tuple[str, ...]
    extra_walk_fn: Callable | None = None
    config_version: int = 1


PYTHON_CONFIG = LanguageConfig(
    name="python",
    grammar="tree_sitter_python",
    class_types=("class_definition",),
    function_types=("function_definition",),
    import_types=("import_statement", "import_from_statement"),
    call_types=("call",),
    call_function_field="function",
    accessor_node_types=("attribute",),
    receiver_types=("python",),
    file_suffixes=(".py",),
    extra_walk_fn=None,
    config_version=1,
)

JAVA_CONFIG = LanguageConfig(
    name="java",
    grammar="tree_sitter_java",
    class_types=("class_declaration",),
    function_types=("method_declaration",),
    import_types=("import_declaration",),
    call_types=("method_invocation",),
    call_function_field="name",
    accessor_node_types=("field_access",),
    receiver_types=("java",),
    file_suffixes=(".java",),
    extra_walk_fn=None,
    config_version=1,
)

LANGUAGE_CONFIGS: dict[str, LanguageConfig] = {
    "python": PYTHON_CONFIG,
    "java": JAVA_CONFIG,
}


def config_for_suffix(suffix: str) -> LanguageConfig | None:
    for cfg in LANGUAGE_CONFIGS.values():
        if suffix in cfg.file_suffixes:
            return cfg
    return None


def grammar_fingerprint(cfg: LanguageConfig) -> str:
    try:
        import tree_sitter

        runtime = getattr(tree_sitter, "__version__", "unknown")
    except Exception:
        runtime = "unknown"
    try:
        wheel = importlib.metadata.version(cfg.grammar)
    except Exception:
        wheel = "unknown"
    blob = "\x00".join(
        [
            runtime,
            wheel,
            str(cfg.config_version),
            ",".join(cfg.class_types),
            ",".join(cfg.function_types),
            ",".join(cfg.import_types),
            ",".join(cfg.call_types),
            cfg.call_function_field,
            ",".join(cfg.accessor_node_types),
        ]
    )
    return hashlib.sha1(blob.encode()).hexdigest()


def load_language(cfg: LanguageConfig):
    from tree_sitter import Language

    mod = importlib.import_module(cfg.grammar)
    return Language(mod.language())


def check_config(cfg: LanguageConfig) -> None:
    if not cfg.class_types or not cfg.function_types:
        raise ValueError(f"empty declaration types for {cfg.name}")
    if not cfg.call_types or not cfg.import_types:
        raise ValueError(f"empty call/import types for {cfg.name}")
    if not cfg.file_suffixes or not all(s.startswith(".") for s in cfg.file_suffixes):
        raise ValueError(f"bad suffixes for {cfg.name}")
    load_language(cfg)
