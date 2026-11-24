"""Shared vector identity, tenant and batching helpers."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from libref.store.vec_store.meta import MetaWriter

_WRITE_BATCH = 512


@runtime_checkable
class VectorStore(Protocol):
    """Surface every vector backend implements; callers stay duck-typed."""

    collection: str

    def ensure_collection(self, session: MetaWriter, model: str, dim: int) -> None: ...

    def upsert_batch(self, points: list, *, wait: bool = True) -> None: ...

    def delete_docs(self, doc_ids: list[int]) -> None: ...

    def delete_version(self, version_id: int, tenant_id: str | None = None) -> None: ...

    def search(
        self,
        query_vec: list[float],
        *,
        k: int,
        version_id: int,
        tenant_id: str | None = None,
    ) -> list[tuple[int, float]]: ...

    def count(self, version_id: int, tenant_id: str | None = None) -> int: ...

    def list_ids(
        self, version_id: int, *, batch: int = 512, tenant_id: str | None = None
    ) -> list[int]: ...

    def diagnostics(self) -> dict[str, Any]:
        """Backend-neutral health snapshot: reachability, collection state, profile, point count."""
        ...

    def apply_tuning(self) -> dict[str, Any]:
        """Rewrite the tuning profile segment in place; empty when the backend cannot."""
        ...

    def optimize(self, *, wait: bool = True) -> dict[str, Any]:
        """Publish pending maintenance now instead of waiting for the next background pass."""
        ...


def _tenant_scoped(tenant_id: str | None) -> bool:
    """Whether tenant filtering applies; global and missing mean unfiltered."""
    if tenant_id is None or tenant_id == "*":
        return False
    return str(tenant_id) != "*"


def _normalize_tenant_id(tenant_id: str | None) -> str:
    return (tenant_id or "default").strip() or "default"


def _split_point(point):
    """Accept 4-tuple legacy points and 5-tuple tenant points."""
    if len(point) == 5:
        doc_id, library_id, version_id, embedding, tenant = point
        return (
            int(doc_id),
            int(library_id),
            int(version_id),
            list(embedding),
            _normalize_tenant_id(tenant),
        )
    doc_id, library_id, version_id, embedding = point
    return int(doc_id), int(library_id), int(version_id), list(embedding), "default"


def paired_vector(
    vec_store: Any | None, embed: Any | None
) -> tuple[Any | None, Any | None]:
    """Couple a vector backend with its embedding function; a half-wired pair disables both."""
    if vec_store is not None and embed is not None:
        return vec_store, embed
    return None, None


def _simvec_id(doc_id: int) -> str:
    """Map a document row id to the string form the local engine stores."""
    if isinstance(doc_id, bool) or not isinstance(doc_id, int) or doc_id <= 0:
        raise ValueError(f"invalid document id for simvec point: {doc_id!r}")
    return str(doc_id)


def _doc_id_from_simvec(raw: Any) -> int:
    """Map a local engine point id back to a document row id; malformed ids are rejected."""
    try:
        value = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid simvec point id: {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"invalid simvec point id: {raw!r}")
    return value
