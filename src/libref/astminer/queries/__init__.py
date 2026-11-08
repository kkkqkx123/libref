"""查询注册表：按语言与查询类型取模式文本，统一指纹片段。"""

from __future__ import annotations

import hashlib

from libref.astminer.queries import java as _java
from libref.astminer.queries import javascript as _javascript
from libref.astminer.queries import python as _python

QUERY_VERSION = 1

KINDS = ("entity", "call", "dependency", "comment")

_MODULES = {
    "python": _python,
    "java": _java,
    "javascript": _javascript,
}


def get(language: str, kind: str) -> str:
    module = _MODULES[language]
    return getattr(module, f"{kind}_query")()


def texts(language: str) -> dict[str, str]:
    return {kind: get(language, kind) for kind in KINDS}


def fingerprint_fragment(language: str) -> str:
    blob = "\x00".join(
        [str(QUERY_VERSION)] + [texts(language)[kind] for kind in KINDS]
    )
    return hashlib.sha1(blob.encode()).hexdigest()
