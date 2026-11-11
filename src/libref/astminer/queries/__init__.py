"""Query registry: fetch pattern text by language and query type, with a unified fingerprint snippet."""

from __future__ import annotations

import hashlib

from libref.astminer.queries import csharp as _csharp
from libref.astminer.queries import go as _go
from libref.astminer.queries import groovy as _groovy
from libref.astminer.queries import java as _java
from libref.astminer.queries import javascript as _javascript
from libref.astminer.queries import kotlin as _kotlin
from libref.astminer.queries import python as _python
from libref.astminer.queries import rust as _rust

QUERY_VERSION = 1

KINDS = ("entity", "call", "dependency", "comment")

_MODULES = {
    "python": _python,
    "java": _java,
    "csharp": _csharp,
    "go": _go,
    "javascript": _javascript,
    "rust": _rust,
    "kotlin": _kotlin,
    "groovy": _groovy,
}


def get(language: str, kind: str) -> str:
    module = _MODULES[language]
    return getattr(module, f"{kind}_query")()


def texts(language: str) -> dict[str, str]:
    return {kind: get(language, kind) for kind in KINDS}


def fingerprint_fragment(language: str) -> str:
    blob = "\x00".join([str(QUERY_VERSION)] + [texts(language)[kind] for kind in KINDS])
    return hashlib.sha1(blob.encode()).hexdigest()
