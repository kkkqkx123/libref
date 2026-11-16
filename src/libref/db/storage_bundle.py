"""Storage bundle: the single handle exits and services receive.

A bundle owns one metadata session, one full-text handle and one vector
handle. Services depend on the bundle rather than on individual handles so
that backend selection happens in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from libref.db.contract import FulltextStore, MetadataSession


@runtime_checkable
class StorageBundle(Protocol):
    """Backend-neutral storage surface consumed by services."""

    metadata: MetadataSession
    fulltext: FulltextStore
    vector: Any  # VectorStore Protocol lives in store.vec_store; keep import lazy

    def diagnostics(self) -> dict[str, Any]: ...


@dataclass
class Bundle:
    """Concrete bundle implementation."""

    metadata: MetadataSession
    fulltext: FulltextStore
    vector: Any

    def diagnostics(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata.diagnostics(),
            "fulltext": self.fulltext.diagnostics(),
            "vector": getattr(self.vector, "diagnostics", dict)(),
        }
