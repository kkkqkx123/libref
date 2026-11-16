"""Vector index wrapper, a derived index over the SQLite source of truth.

Vector point ids map one-to-one to document row ids; the payload stores only filter
keys and body text is fetched back from SQLite. Callers are the orchestration layer;
the API layer never touches this directly. Two backends share one surface:
the remote Qdrant service and the local crates/simvec engine.

A store instance is bound to one collection profile, split into two segments by
what a change costs. The **shape** segment (distance, on-disk placement, shard and
replica counts) only exists at creation time, so drift rebuilds the collection. The
**tuning** segment (graph build parameters, optimizer thresholds) is applied in
place on backends that support it. Both segments are recorded next to the model and
dimension metadata, so drift is detected instead of silently ignored.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable


def collection_for(base: str, db_path: str | os.PathLike[str]) -> str:
    """Derive the collection name from the database file identity; different db files are naturally isolated, in-memory dbs keep the base name for tests only."""
    raw = os.fspath(db_path)
    if raw == ":memory:":
        return base
    digest = hashlib.sha1(os.path.abspath(raw).encode("utf-8")).hexdigest()[:12]
    return f"{base}_{digest}"


def collection_for_deployment(
    base: str, deployment_id: str, db_path: str | os.PathLike[str]
) -> str:
    """Derive the collection name from deployment identity when declared.

    Remote deployments share one vector service across hosts, so a local
    database file path is meaningless as identity; the explicit deployment
    identifier is used instead. Without one, naming falls back to the file
    identity for embedded single-host runs.
    """
    deployment = str(deployment_id or "").strip()
    if deployment:
        digest = hashlib.sha1(deployment.encode("utf-8")).hexdigest()[:12]
        return f"{base}_{digest}"
    return collection_for(base, db_path)


_WRITE_BATCH = 512
"""Default points per backend call; each store takes its batch size at construction."""


@runtime_checkable
class MetaWriter(Protocol):
    """Metadata write handle a vector backend needs for its own bookkeeping.

    Satisfied by the embedded driver connection and by the backend-neutral
    metadata session, so collection governance never names a driver type.
    """

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any: ...

    def executemany(self, sql: str, seq: Iterable[tuple[Any, ...]]) -> Any: ...

    def commit(self) -> None: ...


INDEX_TIERS = ("hnsw", "flat")
"""Local index tiers: exact scan with self-promotion, or exact scan forever."""

DISTANCES = ("cosine", "euclid", "dot", "manhattan")
"""Distance names shared by configuration and both backends."""


class VectorCapacityError(RuntimeError):
    """A write would push the collection past its configured point ceiling.

    Deliberately a hard stop: the vector index is a derived store, so the source of
    truth is untouched and the batch can be replayed once capacity is raised.
    """


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


@dataclass(frozen=True)
class VectorProfile:
    """Collection shape and tuning the derived index is built with.

    The two segments differ in what a change costs. ``shape_identity`` can only be
    decided when the collection is created, so drift there rebuilds it.
    ``tuning_identity`` can be rewritten in place by the remote backend, so drift
    there is applied with an update and existing points survive. The local engine
    fixes its graph parameters at creation time, so they stay in the shape segment.

    A ``None`` value means "whatever the backend defaults to" and stays out of both
    segments, which keeps an unconfigured deployment byte-identical to the defaults.
    """

    backend: str = "simvec"
    distance: str = "cosine"
    index: str = "hnsw"
    hnsw_m: int = 16
    hnsw_ef_construct: int = 100
    full_scan_threshold: int = 10000
    ef_search: int = 40
    max_indexing_threads: int | None = None

    # Remote backend, shape segment: creation-only, a change rebuilds.
    qdrant_on_disk_vectors: bool | None = None
    qdrant_on_disk_payload: bool | None = None
    qdrant_shard_number: int | None = None
    qdrant_replication_factor: int | None = None
    qdrant_write_consistency_factor: int | None = None

    # Remote backend, tuning segment: applied in place, points survive.
    qdrant_hnsw_m: int | None = None
    qdrant_hnsw_ef_construct: int | None = None
    qdrant_full_scan_threshold: int | None = None
    qdrant_memmap_threshold_kb: int | None = None
    qdrant_indexing_threshold_kb: int | None = None

    def shape_identity(self) -> dict[str, Any]:
        """Backend-relevant subset that only creation can change."""
        ident: dict[str, Any] = {"backend": self.backend, "distance": self.distance}
        if self.backend == "simvec":
            ident["index"] = self.index
            if self.index == "hnsw":
                hnsw: dict[str, Any] = {
                    "m": self.hnsw_m,
                    "ef_construct": self.hnsw_ef_construct,
                    "full_scan_threshold": self.full_scan_threshold,
                    "ef_search": self.ef_search,
                }
                if self.max_indexing_threads:
                    hnsw["max_indexing_threads"] = self.max_indexing_threads
                ident["hnsw"] = hnsw
            return ident
        for key in (
            "qdrant_on_disk_vectors",
            "qdrant_on_disk_payload",
            "qdrant_shard_number",
            "qdrant_replication_factor",
            "qdrant_write_consistency_factor",
        ):
            value = getattr(self, key)
            if value is not None:
                ident[key.removeprefix("qdrant_")] = value
        return ident

    def tuning_identity(self) -> dict[str, Any]:
        """Subset the remote backend can rewrite in place; the local engine has none."""
        if self.backend != "qdrant":
            return {}
        tuning: dict[str, Any] = {}
        for key, member in (
            ("qdrant_hnsw_m", "hnsw_m"),
            ("qdrant_hnsw_ef_construct", "hnsw_ef_construct"),
            ("qdrant_full_scan_threshold", "full_scan_threshold"),
            ("qdrant_memmap_threshold_kb", "memmap_threshold_kb"),
            ("qdrant_indexing_threshold_kb", "indexing_threshold_kb"),
        ):
            value = getattr(self, key)
            if value is not None:
                tuning[member] = value
        return tuning

    def identity(self) -> dict[str, Any]:
        """Full recorded profile: both segments, so diagnostics show what is in force."""
        ident = self.shape_identity()
        tuning = self.tuning_identity()
        if tuning:
            ident["tuning"] = tuning
        return ident

    def fingerprint(self) -> str:
        """Digest of the shape segment; equal shapes share it, any shape change breaks it."""
        payload = json.dumps(self.shape_identity(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def tuning_fingerprint(self) -> str:
        """Digest of the tuning segment; drift is applied in place, never rebuilt."""
        payload = json.dumps(self.tuning_identity(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def as_meta(self) -> str:
        """Serialized full profile for schema_meta."""
        return json.dumps(self.identity(), sort_keys=True)

    def shape_meta(self) -> str:
        """Serialized shape segment, compared against the recorded one to decide rebuilds."""
        return json.dumps(self.shape_identity(), sort_keys=True)

    def tuning_meta(self) -> str:
        """Serialized tuning segment, compared against the recorded one to decide retunes."""
        return json.dumps(self.tuning_identity(), sort_keys=True)

    def for_backend(self, backend: str) -> VectorProfile:
        """Same shape settings bound to another backend."""
        return VectorProfile(
            backend=backend,
            distance=self.distance,
            index=self.index,
            hnsw_m=self.hnsw_m,
            hnsw_ef_construct=self.hnsw_ef_construct,
            full_scan_threshold=self.full_scan_threshold,
            ef_search=self.ef_search,
            max_indexing_threads=self.max_indexing_threads,
            qdrant_on_disk_vectors=self.qdrant_on_disk_vectors,
            qdrant_on_disk_payload=self.qdrant_on_disk_payload,
            qdrant_shard_number=self.qdrant_shard_number,
            qdrant_replication_factor=self.qdrant_replication_factor,
            qdrant_write_consistency_factor=self.qdrant_write_consistency_factor,
            qdrant_hnsw_m=self.qdrant_hnsw_m,
            qdrant_hnsw_ef_construct=self.qdrant_hnsw_ef_construct,
            qdrant_full_scan_threshold=self.qdrant_full_scan_threshold,
            qdrant_memmap_threshold_kb=self.qdrant_memmap_threshold_kb,
            qdrant_indexing_threshold_kb=self.qdrant_indexing_threshold_kb,
        )


def _needs_recreate(
    cur_dim: int | None, dim: int, stored_shape: str | None, profile: VectorProfile
) -> bool:
    """Whether the live collection shape forces a rebuild.

    Dimension drift always rebuilds, and a missing shape record counts as drift so
    collections written before the profile was split are rebuilt instead of trusted.
    Model-name drift alone never rebuilds; the maintenance layer repairs it per version.
    """
    if cur_dim is None or cur_dim != dim:
        return True
    return stored_shape != profile.shape_meta()


def _needs_retune(stored_tuning: str | None, profile: VectorProfile) -> bool:
    """Whether the tuning segment drifted; equal settings never touch a live collection."""
    return stored_tuning != profile.tuning_meta()


def _read_meta(session: MetaWriter, key: str) -> str | None:
    """One bookkeeping value, if this database ever wrote it."""
    try:
        row = session.execute(
            "SELECT value FROM schema_meta WHERE key=?", (key,)
        ).fetchone()
    except Exception:  # noqa: BLE001 — absent bookkeeping table means no record yet
        return None
    return None if row is None else str(row[0])


def _read_profile(session: MetaWriter) -> tuple[str | None, str | None]:
    """Recorded shape and tuning segments; an absent shape means the collection is untrusted."""
    return _read_meta(session, "vec_profile_shape"), _read_meta(
        session, "vec_profile_tuning"
    )


def _utc_stamp() -> str:
    """Timestamp text in the shared column format, generated here so the write stays dialect-free."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _write_vec_meta(
    session: MetaWriter, model: str, dim: int, profile: VectorProfile
) -> None:
    """Record the last writer model plus both profile segments; per-version records stay authoritative."""
    session.executemany(
        "INSERT INTO schema_meta(key, value) VALUES(?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (
            ("vec_model", model),
            ("vec_dim", str(dim)),
            ("vec_profile", profile.as_meta()),
            ("vec_profile_shape", profile.shape_meta()),
            ("vec_profile_tuning", profile.tuning_meta()),
        ),
    )
    session.commit()


def read_version_meta(
    session: MetaWriter, version_id: int
) -> tuple[str | None, int | None]:
    """Per-version embedding record; absent means the version was never embedded under the current scheme."""
    try:
        row = session.execute(
            "SELECT model, dim FROM vec_version_meta WHERE version_id=?",
            (version_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001 — missing table means never embedded
        return None, None
    if row is None:
        return None, None
    return row[0], int(row[1])


def write_version_meta(
    session: MetaWriter, version_id: int, model: str, dim: int
) -> None:
    """Record a version's embedded model; committed with the caller's transaction scope."""
    session.execute(
        "INSERT INTO vec_version_meta(version_id, model, dim, updated_at)"
        " VALUES(?, ?, ?, ?) ON CONFLICT(version_id) DO UPDATE SET"
        " model=excluded.model, dim=excluded.dim, updated_at=excluded.updated_at",
        (version_id, model, dim, _utc_stamp()),
    )
    session.commit()


def _check_capacity(
    collection: str, current: int, incoming: int, limit: int | None
) -> None:
    """Refuse a batch that would exceed the point ceiling.

    A defensive gate rather than a quota: the count includes points the batch may
    overwrite, so the check is conservative by design. The index is derived, so a
    refused batch can be replayed after raising the limit or purging a version.
    """
    if limit is None or incoming <= 0:
        return
    if current + incoming > limit:
        raise VectorCapacityError(
            f"collection {collection} holds {current} points and this batch adds "
            f"{incoming}, past the configured ceiling of {limit}; raise "
            f"vector_max_points or purge versions before writing again"
        )


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


class QdrantVecStore(VectorStore):
    """Remote backend: single collection with payload-version filtering."""

    def __init__(
        self,
        url: str,
        collection: str,
        *,
        timeout: float = 5.0,
        api_key: str | None = None,
        https: bool | None = None,
        prefer_grpc: bool = False,
        grpc_port: int = 6334,
        prefix: str | None = None,
        pool_size: int | None = None,
        search_hnsw_ef: int | None = None,
        search_exact: bool = False,
        write_batch: int = _WRITE_BATCH,
        max_points: int | None = None,
        profile: VectorProfile | None = None,
        client: Any | None = None,
    ) -> None:
        self.collection = collection
        self.profile = (profile or VectorProfile()).for_backend("qdrant")
        self.search_hnsw_ef = search_hnsw_ef
        self.search_exact = bool(search_exact)
        self.write_batch = max(1, int(write_batch))
        self.max_points = max_points
        if client is not None:
            self.client = client
        else:
            from qdrant_client import QdrantClient

            self.client = QdrantClient(
                url=url,
                timeout=int(timeout),
                api_key=api_key,
                https=https,
                prefer_grpc=bool(prefer_grpc),
                grpc_port=int(grpc_port),
                prefix=prefix,
                pool_size=pool_size,
            )

    # ── Collection governance ──

    def ensure_collection(self, session: MetaWriter, model: str, dim: int) -> None:
        """Create or verify the collection and register model metadata.

        When the collection does not exist, create it from the shape and tuning
        segments plus the filter indexes. Shape drift deletes and recreates it;
        tuning drift is applied in place so existing points survive. A model-name
        change alone never wipes other versions and is repaired per version by the
        maintenance layer. An existing collection still gets a payload-index replay,
        so a collection created before an index was declared is repaired instead of
        silently staying unindexed.
        """
        stored_shape, stored_tuning = _read_profile(session)
        if self.client.collection_exists(self.collection):
            cur_dim = self._collection_dim()
            if _needs_recreate(cur_dim, dim, stored_shape, self.profile):
                self.client.delete_collection(self.collection)
        if not self.client.collection_exists(self.collection):
            self._create_collection(dim)
        elif _needs_retune(stored_tuning, self.profile):
            self.apply_tuning()
        self._ensure_payload_indexes()
        _write_vec_meta(session, model, dim, self.profile)

    def _create_collection(self, dim: int) -> None:
        """Create the live collection from both profile segments; unset fields keep service defaults."""
        from qdrant_client import models

        kwargs: dict[str, Any] = {
            "collection_name": self.collection,
            "vectors_config": models.VectorParams(
                size=dim,
                distance=self._distance_enum(),
                on_disk=self.profile.qdrant_on_disk_vectors,
            ),
            "on_disk_payload": self.profile.qdrant_on_disk_payload,
            "shard_number": self.profile.qdrant_shard_number,
            "replication_factor": self.profile.qdrant_replication_factor,
            "write_consistency_factor": self.profile.qdrant_write_consistency_factor,
        }
        hnsw = self._hnsw_config()
        if hnsw is not None:
            kwargs["hnsw_config"] = hnsw
        optimizers = self._optimizers_config()
        if optimizers is not None:
            kwargs["optimizers_config"] = optimizers
        # None means "service default": passing it explicitly would override that default
        self.client.create_collection(
            **{key: value for key, value in kwargs.items() if value is not None}
        )

    def _hnsw_config(self):
        """Graph build parameters, as an in-place-updatable diff; None when unset."""
        from qdrant_client import models

        tuning = self.profile.tuning_identity()
        params = {
            "m": tuning.get("hnsw_m"),
            "ef_construct": tuning.get("hnsw_ef_construct"),
            "full_scan_threshold": tuning.get("full_scan_threshold"),
        }
        if all(value is None for value in params.values()):
            return None
        return models.HnswConfigDiff(**params)

    def _optimizers_config(self):
        """Optimizer thresholds that decide when a segment moves to disk; None when unset."""
        from qdrant_client import models

        tuning = self.profile.tuning_identity()
        params = {
            "memmap_threshold": tuning.get("memmap_threshold_kb"),
            "indexing_threshold": tuning.get("indexing_threshold_kb"),
        }
        if all(value is None for value in params.values()):
            return None
        return models.OptimizersConfigDiff(**params)

    def apply_tuning(self) -> dict[str, Any]:
        """Rewrite the tuning segment on the live collection; no rebuild, no point loss."""
        tuning = self.profile.tuning_identity()
        applied: dict[str, Any] = {}
        hnsw = self._hnsw_config()
        if hnsw is not None:
            self.client.update_collection(
                collection_name=self.collection, hnsw_config=hnsw
            )
            applied["hnsw"] = {
                key: tuning[key]
                for key in ("hnsw_m", "hnsw_ef_construct", "full_scan_threshold")
                if key in tuning
            }
        optimizers = self._optimizers_config()
        if optimizers is not None:
            self.client.update_collection(
                collection_name=self.collection, optimizers_config=optimizers
            )
            applied["optimizers"] = {
                key: tuning[key]
                for key in ("memmap_threshold_kb", "indexing_threshold_kb")
                if key in tuning
            }
        return applied

    def optimize(self, *, wait: bool = True) -> dict[str, Any]:
        """Apply any pending tuning segment; the service optimizes on its own schedule.

        Qdrant exposes no client-side "build the graph now" call: segments are
        optimized by the service worker, and an empty update would schedule nothing.
        This therefore reports the applied tuning and the live point count instead of
        pretending to force a rebuild.
        """
        out: dict[str, Any] = {"backend": "qdrant", "applied": self.apply_tuning()}
        try:
            out["points"] = int(self.client.count(self.collection, exact=True).count)
            # The service owns graph publication; there is no client-side signal for it
            out["graph"] = "service-managed"
        except Exception as exc:  # noqa: BLE001 — a report must not raise
            out["error"] = str(exc)
        if not out["applied"]:
            out["note"] = "service optimizer is autonomous; nothing to apply"
        return out

    def _distance_enum(self):
        from qdrant_client import models

        return {
            "cosine": models.Distance.COSINE,
            "euclid": models.Distance.EUCLID,
            "dot": models.Distance.DOT,
            "manhattan": models.Distance.MANHATTAN,
        }[self.profile.distance]

    def _collection_dim(self) -> int | None:
        from qdrant_client import models

        info = self.client.get_collection(self.collection)
        vectors = info.config.params.vectors
        if isinstance(vectors, models.VectorParams):
            return vectors.size
        return None

    def _ensure_payload_indexes(self) -> None:
        """Replay the declared filter indexes; existing ones are left untouched."""
        from qdrant_client import models

        info = self.client.get_collection(self.collection)
        present = set(getattr(info, "payload_schema", None) or {})
        if "version_id" not in present:
            # version_id is the partition key; the integer principal index keeps
            # equal values linked and lets the filter run before the graph walk
            self.client.create_payload_index(
                collection_name=self.collection,
                field_name="version_id",
                field_schema=models.IntegerIndexParams(
                    type=models.IntegerIndexType.INTEGER, is_principal=True
                ),
            )
        if "library_id" not in present:
            self.client.create_payload_index(
                collection_name=self.collection,
                field_name="library_id",
                field_schema=models.PayloadSchemaType.INTEGER,
            )
        if "tenant_id" not in present:
            self.client.create_payload_index(
                collection_name=self.collection,
                field_name="tenant_id",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

    # ── Write ──

    def upsert_batch(
        self,
        points: list,
        *,
        wait: bool = True,
    ) -> None:
        """Batch-write document, library and version ids with vectors, idempotent overwrite."""
        from qdrant_client import models

        if not points:
            return
        if self.max_points is not None:
            _check_capacity(
                self.collection,
                int(self.client.count(self.collection, exact=True).count),
                len(points),
                self.max_points,
            )
        for start in range(0, len(points), self.write_batch):
            chunk = points[start : start + self.write_batch]
            payload_points = []
            for point in chunk:
                doc_id, library_id, version_id, embedding, tenant = _split_point(point)
                payload_points.append(
                    models.PointStruct(
                        id=doc_id,
                        vector=embedding,
                        payload={
                            "library_id": library_id,
                            "version_id": version_id,
                            "tenant_id": tenant,
                        },
                    )
                )
            self.client.upsert(
                collection_name=self.collection,
                points=payload_points,
                wait=wait,
            )

    def delete_docs(self, doc_ids: list[int]) -> None:
        """Delete vector points for the given documents, cleaning up old points on single-page re-chunking."""
        from qdrant_client import models

        if not doc_ids:
            return
        for start in range(0, len(doc_ids), self.write_batch):
            self.client.delete(
                collection_name=self.collection,
                points_selector=models.PointIdsList(
                    points=cast(Any, doc_ids[start : start + self.write_batch])
                ),
            )

    def delete_version(self, version_id: int, tenant_id: str | None = None) -> None:
        """Delete all vector points of a version, on re-crawl of that version."""
        from qdrant_client import models

        self.client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=self._version_filter(version_id, tenant_id)
            ),
        )

    # ── Search and reconciliation ──

    def search(
        self,
        query_vec: list[float],
        *,
        k: int,
        version_id: int,
        tenant_id: str | None = None,
    ) -> list[tuple[int, float]]:
        """Nearest-neighbor search filtered by version; returns document ids and cosine similarity in descending similarity order."""
        filt = self._version_filter(version_id, tenant_id)
        want_payload = _tenant_scoped(tenant_id)
        res = self.client.query_points(
            collection_name=self.collection,
            query=query_vec,
            query_filter=filt,
            limit=max(k, k * 3)
            if (want_payload and _normalize_tenant_id(tenant_id) == "default")
            else k,
            with_payload=want_payload,
            search_params=self._search_params(),
        )
        if not want_payload:
            return [(p.id, p.score) for p in res.points]
        wanted = _normalize_tenant_id(tenant_id)
        out: list[tuple[int, float]] = []
        for p in res.points:
            payload = getattr(p, "payload", None) or {}
            seen = str(payload.get("tenant_id") or "default")
            if wanted == "default":
                if seen != "default":
                    continue
            elif seen != wanted:
                continue
            out.append((p.id, p.score))
            if len(out) >= k:
                break
        return out

    def count(self, version_id: int, tenant_id: str | None = None) -> int:
        """Number of vector points in a version, for reconciliation against the embeddable subset."""
        if not _tenant_scoped(tenant_id):
            return self.client.count(
                collection_name=self.collection,
                count_filter=self._version_filter(version_id, None),
                exact=True,
            ).count
        if _normalize_tenant_id(tenant_id) != "default":
            return self.client.count(
                collection_name=self.collection,
                count_filter=self._version_filter(version_id, tenant_id),
                exact=True,
            ).count
        ids = self.list_ids(version_id, tenant_id=tenant_id)
        return len(ids)

    def list_ids(
        self, version_id: int, *, batch: int = 512, tenant_id: str | None = None
    ) -> list[int]:
        """All vector point ids of a version, paged scroll for set-difference repair."""
        if not _tenant_scoped(tenant_id):
            found: list[int] = []
            offset = None
            while True:
                records, offset = self.client.scroll(
                    collection_name=self.collection,
                    scroll_filter=self._version_filter(version_id, None),
                    limit=max(1, int(batch)),
                    offset=offset,
                    with_payload=False,
                    with_vectors=False,
                )
                found.extend(int(r.id) for r in records)
                if offset is None:
                    return found
        wanted = _normalize_tenant_id(tenant_id)
        found_scoped: list[int] = []
        offset2 = None
        while True:
            records, offset2 = self.client.scroll(
                collection_name=self.collection,
                scroll_filter=self._version_filter(version_id, tenant_id),
                limit=max(1, int(batch)),
                offset=offset2,
                with_payload=True,
                with_vectors=False,
            )
            for r in records:
                payload = getattr(r, "payload", None) or {}
                seen = str(payload.get("tenant_id") or "default")
                if wanted == "default" and seen != "default":
                    continue
                if wanted != "default" and seen != wanted:
                    continue
                found_scoped.append(int(r.id))
            if offset2 is None:
                return found_scoped

    @staticmethod
    def _version_filter(version_id: int, tenant_id: str | None = None):
        from qdrant_client import models

        version_cond = models.FieldCondition(
            key="version_id", match=models.MatchValue(value=version_id)
        )
        if not _tenant_scoped(tenant_id):
            return models.Filter(must=[version_cond])
        wanted = _normalize_tenant_id(tenant_id)
        if wanted == "default":
            from qdrant_client.models import IsNullCondition, PayloadField

            return models.Filter(
                must=[version_cond],
                should=[
                    models.FieldCondition(
                        key="tenant_id", match=models.MatchValue(value="default")
                    ),
                    IsNullCondition(is_null=PayloadField(key="tenant_id")),
                ],
            )
        return models.Filter(
            must=[
                version_cond,
                models.FieldCondition(
                    key="tenant_id", match=models.MatchValue(value=wanted)
                ),
            ]
        )

    def _search_params(self):
        """Search-time knobs; an unset pair keeps the service defaults."""
        if self.search_hnsw_ef is None and not self.search_exact:
            return None
        from qdrant_client import models

        return models.SearchParams(hnsw_ef=self.search_hnsw_ef, exact=self.search_exact)

    def diagnostics(self) -> dict[str, Any]:
        """Backend-neutral health snapshot for the remote vector service."""
        info: dict[str, Any] = {
            "backend": "qdrant",
            "collection": self.collection,
            "profile": self.profile.identity(),
            "profile_fingerprint": self.profile.fingerprint(),
            "tuning_fingerprint": self.profile.tuning_fingerprint(),
            "write_batch": self.write_batch,
            "max_points": self.max_points,
            "search": {
                "hnsw_ef": self.search_hnsw_ef,
                "exact": self.search_exact,
            },
        }
        try:
            exists = self.client.collection_exists(self.collection)
            info["collection_exists"] = exists
            if exists:
                res = self.client.count(collection_name=self.collection, exact=True)
                info["points"] = int(getattr(res, "count", 0) or 0)
                dim = self._collection_dim()
                if dim is not None:
                    info["dim"] = dim
                schema = getattr(
                    self.client.get_collection(self.collection), "payload_schema", None
                )
                info["payload_indexes"] = sorted(schema or ())
            info["reachable"] = True
        except Exception as exc:  # noqa: BLE001 — diagnostics must not raise
            info["reachable"] = False
            info["error"] = str(exc)
        return info


# ── Local backend over the crates/simvec submodule ──


class SimvecLoadError(Exception):
    """Native simvec library or binding unavailable; the message names how to build it."""


def _simvec_bindings_dir() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "crates"
        / "simvec"
        / "bindings"
        / "python"
    )


def _import_simvec_binding(lib: str | os.PathLike[str] | None = None):
    """Import the ctypes binding scoped to this loader; explicit path beats environment beats default discovery."""
    import importlib

    if lib is not None:
        os.environ["SIMVEC_LIB"] = os.fspath(lib)
    bindings = str(_simvec_bindings_dir())
    inserted = bindings not in sys.path
    if inserted:
        sys.path.insert(0, bindings)
    try:
        return importlib.import_module("simvec")
    except Exception as exc:
        raise SimvecLoadError(
            "simvec native library not found; build it with "
            "cargo build --features c_api "
            "--manifest-path crates/simvec/Cargo.toml "
            "or point simvec_lib/SIMVEC_LIB at libsimvec explicitly"
        ) from exc
    finally:
        if inserted:
            with contextlib.suppress(ValueError):
                sys.path.remove(bindings)


_ENGINES: dict[str, Any] = {}
_ENGINES_LOCK = threading.Lock()
_MEM_SIMVEC_ROOT: Path | None = None


def simvec_available(lib: str | os.PathLike[str] | None = None) -> bool:
    """Whether the native simvec binding imports; used for test skip decisions."""
    try:
        _import_simvec_binding(lib)
    except SimvecLoadError:
        return False
    return True


def _open_simvec_engine(root: Path, lib: str | os.PathLike[str] | None = None):
    key = os.path.abspath(os.fspath(root))
    with _ENGINES_LOCK:
        engine = _ENGINES.get(key)
        if engine is None:
            os.makedirs(key, exist_ok=True)
            engine = _import_simvec_binding(lib).Engine(key)
            _ENGINES[key] = engine
        return engine


def close_simvec_engines() -> None:
    """Release cached engines; used by tests and process teardown."""
    while True:
        with _ENGINES_LOCK:
            item = _ENGINES.popitem() if _ENGINES else None
        if item is None:
            return
        with contextlib.suppress(Exception):
            item[1].close()


def simvec_root_for(db_path, simvec_dir=None) -> Path:
    """Derive the engine root from the database file; explicit directory wins, memory databases share one cached root."""
    global _MEM_SIMVEC_ROOT
    if simvec_dir is not None:
        return Path(os.fspath(simvec_dir))
    raw = os.fspath(db_path)
    if raw == ":memory:":
        with _ENGINES_LOCK:
            if _MEM_SIMVEC_ROOT is None:
                _MEM_SIMVEC_ROOT = Path(tempfile.mkdtemp(prefix="libref-simvec-"))
            return _MEM_SIMVEC_ROOT
    return Path(os.path.abspath(raw) + ".simvec")


def _simvec_version_filter(
    version_id: int, tenant_id: str | None = None
) -> dict[str, Any]:
    # Match values cross the boundary as strings; numeric payloads compare by decimal form.
    base: dict[str, Any] = {
        "must": [
            {"field": "version_id", "condition": {"Match": {"value": str(version_id)}}}
        ]
    }
    if _tenant_scoped(tenant_id):
        wanted = _normalize_tenant_id(tenant_id)
        # Non-default tenants push both keys; default relies on over-fetch plus payload filtering
        # so legacy points without a tenant field stay visible to the default tenant.
        if wanted != "default":
            base["must"].append(
                {"field": "tenant_id", "condition": {"Match": {"value": wanted}}}
            )
    return base


def _simvec_tenant_ok(payload: dict | None, tenant_id: str | None) -> bool:
    if not _tenant_scoped(tenant_id):
        return True
    wanted = _normalize_tenant_id(tenant_id)
    seen = str((payload or {}).get("tenant_id") or "default")
    return seen == wanted


class SimvecVecStore(VectorStore):
    """Local backend: single collection with payload-version filtering."""

    def __init__(
        self,
        root: str | Path,
        collection: str,
        *,
        lib: str | os.PathLike[str] | None = None,
        write_batch: int = _WRITE_BATCH,
        max_points: int | None = None,
        profile: VectorProfile | None = None,
    ) -> None:
        self.root = Path(os.fspath(root))
        self.collection = collection
        self.profile = (profile or VectorProfile()).for_backend("simvec")
        self.write_batch = max(1, int(write_batch))
        self.max_points = max_points
        self._engine = _open_simvec_engine(self.root, lib)

    def ensure_collection(self, session: MetaWriter, model: str, dim: int) -> None:
        """Create or verify the collection and register model metadata, mirroring the remote rebuild semantics.

        The live shape is read back from the engine for the dimension, while the
        stored profile decides index-tier drift; an existing collection always gets
        a payload-index replay so a missing filter index is repaired, not ignored.
        """
        stored_shape, _stored_tuning = _read_profile(session)
        if not self._engine.collection_exists(self.collection):
            self._create_collection(dim)
        else:
            try:
                cur = self._engine.collection_config(self.collection)
                cur_dim = cur.get("vector_size")
            except Exception:  # noqa: BLE001 — unreadable shape counts as mismatch, rebuilt below
                cur_dim = None
            if _needs_recreate(cur_dim, dim, stored_shape, self.profile):
                self._engine.delete_collection(self.collection)
                self._create_collection(dim)
        self._ensure_payload_indexes()
        _write_vec_meta(session, model, dim, self.profile)

    def apply_tuning(self) -> dict[str, Any]:
        """No in-place tuning exists locally: graph parameters are fixed at creation time."""
        return {}

    def optimize(self, *, wait: bool = True) -> dict[str, Any]:
        """Publish the graph now instead of waiting for the engine's 30 s maintenance tick.

        Ordering matters: compaction first frees slots and rewrites them, so a graph
        built afterwards cannot reference stale slots. The graph is only published once
        the live count reaches ``full_scan_threshold``, matching the engine's own
        promotion rule; below it, exact scan is both faster and exact.
        """
        out: dict[str, Any] = {"backend": "simvec", "collection": self.collection}
        if not self._engine.collection_exists(self.collection):
            out["skipped"] = "collection does not exist"
            return out
        if not wait:
            out["skipped"] = "the local engine only runs maintenance synchronously"
            return out
        out["compacted"] = int(self._engine.compact(self.collection))
        self._engine.drain_pending(self.collection)
        points = int(self._engine.count(self.collection))
        built = False
        threshold = (
            self.profile.full_scan_threshold if self.profile.index == "hnsw" else None
        )
        if (
            threshold is not None
            and points >= threshold
            and not self._engine.has_index(self.collection)
        ):
            built = bool(self._engine.build_index(self.collection))
            # Points appended while the graph was building are adopted by the drain
            self._engine.drain_pending(self.collection)
        out["index_built"] = built
        out["points"] = points
        out["has_index"] = bool(self._engine.has_index(self.collection))
        out["threshold"] = threshold
        return out

    def _collection_config(self, dim: int) -> dict[str, Any]:
        """Collection request for the configured profile; the tier decides the index payload."""
        config: dict[str, Any] = {
            "vector_size": dim,
            "distance": {
                "cosine": "Cosine",
                "euclid": "Euclid",
                "dot": "Dot",
                "manhattan": "Manhattan",
            }[self.profile.distance],
        }
        if self.profile.index == "flat":
            config["index"] = "Flat"
            return config
        hnsw: dict[str, Any] = {
            "m": self.profile.hnsw_m,
            "ef_construct": self.profile.hnsw_ef_construct,
            "full_scan_threshold": self.profile.full_scan_threshold,
            "ef_search": self.profile.ef_search,
        }
        if self.profile.max_indexing_threads:
            hnsw["max_indexing_threads"] = self.profile.max_indexing_threads
        # The tier enum is externally tagged, so the struct variant carries its name.
        config["index"] = {"Hnsw": hnsw}
        return config

    def _create_collection(self, dim: int) -> None:
        self._engine.create_collection(self.collection, self._collection_config(dim))
        self._ensure_payload_indexes()

    def _ensure_payload_indexes(self) -> None:
        """Replay the declared filter indexes; the engine rejects duplicates, so existing fields are skipped."""
        indexed = {
            str(item.get("field"))
            for item in self._engine.list_payload_indexes(self.collection)
        }
        for field in ("version_id", "library_id", "tenant_id"):
            if field in indexed:
                continue
            schema = "keyword" if field == "tenant_id" else "integer"
            self._engine.create_payload_index(self.collection, field, schema)

    def upsert_batch(
        self,
        points: list,
        *,
        wait: bool = True,
    ) -> None:
        """Batch-write document, library and version ids with vectors, idempotent overwrite; local writes are visible on return."""
        if not points:
            return
        if self.max_points is not None:
            _check_capacity(
                self.collection,
                int(self._engine.count(self.collection)),
                len(points),
                self.max_points,
            )
        for start in range(0, len(points), self.write_batch):
            chunk = points[start : start + self.write_batch]
            payload_batch = []
            for point in chunk:
                doc_id, library_id, version_id, embedding, tenant = _split_point(point)
                payload_batch.append(
                    {
                        "id": _simvec_id(doc_id),
                        "vector": list(embedding),
                        "payload": {
                            "library_id": library_id,
                            "version_id": version_id,
                            "tenant_id": tenant,
                        },
                    }
                )
            self._engine.upsert_batch(
                self.collection,
                payload_batch,
            )

    def delete_docs(self, doc_ids: list[int]) -> None:
        """Delete vector points for the given documents, cleaning up old points on single-page re-chunking."""
        if not doc_ids:
            return
        for start in range(0, len(doc_ids), self.write_batch):
            self._engine.delete_batch(
                self.collection,
                [_simvec_id(i) for i in doc_ids[start : start + self.write_batch]],
            )

    def delete_version(self, version_id: int, tenant_id: str | None = None) -> None:
        """Delete all vector points of a version, on re-crawl of that version."""
        if not _tenant_scoped(tenant_id):
            self._engine.delete_by_filter(
                self.collection, _simvec_version_filter(version_id)
            )
            return
        wanted = _normalize_tenant_id(tenant_id)
        if wanted != "default":
            self._engine.delete_by_filter(
                self.collection, _simvec_version_filter(version_id, tenant_id)
            )
            return
        ids = self.list_ids(version_id, tenant_id=tenant_id)
        if ids:
            self.delete_docs(ids)

    def search(
        self,
        query_vec: list[float],
        *,
        k: int,
        version_id: int,
        tenant_id: str | None = None,
    ) -> list[tuple[int, float]]:
        """Nearest-neighbor search filtered by version; returns document ids and cosine similarity in descending similarity order."""
        if not _tenant_scoped(tenant_id):
            hits = self._engine.search_vector(
                self.collection,
                list(query_vec),
                k,
                query_filter=_simvec_version_filter(version_id),
                with_payload=False,
                with_vector=False,
            )
            return [(_doc_id_from_simvec(h["id"]), float(h["score"])) for h in hits]
        wanted = _normalize_tenant_id(tenant_id)
        if wanted != "default":
            hits = self._engine.search_vector(
                self.collection,
                list(query_vec),
                k,
                query_filter=_simvec_version_filter(version_id, tenant_id),
                with_payload=True,
                with_vector=False,
            )
            out = []
            for h in hits:
                if not _simvec_tenant_ok(h.get("payload"), tenant_id):
                    continue
                out.append((_doc_id_from_simvec(h["id"]), float(h["score"])))
                if len(out) >= k:
                    break
            return out
        fetch_k = max(k * 5, k + 20)
        hits = self._engine.search_vector(
            self.collection,
            list(query_vec),
            fetch_k,
            query_filter=_simvec_version_filter(version_id),
            with_payload=True,
            with_vector=False,
        )
        out2: list[tuple[int, float]] = []
        for h in hits:
            if not _simvec_tenant_ok(h.get("payload"), tenant_id):
                continue
            out2.append((_doc_id_from_simvec(h["id"]), float(h["score"])))
            if len(out2) >= k:
                break
        return out2

    def count(self, version_id: int, tenant_id: str | None = None) -> int:
        """Number of vector points in a version, for reconciliation against the embeddable subset."""
        if not _tenant_scoped(tenant_id):
            return int(
                self._engine.count_by_filter(
                    self.collection, _simvec_version_filter(version_id)
                )
            )
        if _normalize_tenant_id(tenant_id) != "default":
            return int(
                self._engine.count_by_filter(
                    self.collection, _simvec_version_filter(version_id, tenant_id)
                )
            )
        return len(self.list_ids(version_id, tenant_id=tenant_id))

    def list_ids(
        self, version_id: int, *, batch: int = 512, tenant_id: str | None = None
    ) -> list[int]:
        """All vector point ids of a version, paged scroll for set-difference repair."""
        if not _tenant_scoped(tenant_id):
            found: list[int] = []
            offset = None
            while True:
                page = self._engine.scroll_by_filter(
                    self.collection,
                    _simvec_version_filter(version_id),
                    max(1, int(batch)),
                    offset,
                    with_payload=False,
                    with_vector=False,
                )
                for point in page.get("points", []):
                    found.append(_doc_id_from_simvec(point["id"]))
                offset = page.get("next_offset")
                if not offset:
                    return found
        filt = _simvec_version_filter(version_id, tenant_id)
        want_payload = True
        scoped: list[int] = []
        off = None
        while True:
            pg = self._engine.scroll_by_filter(
                self.collection,
                filt,
                max(1, int(batch)),
                off,
                with_payload=want_payload,
                with_vector=False,
            )
            for point in pg.get("points", []):
                if not _simvec_tenant_ok(point.get("payload"), tenant_id):
                    continue
                scoped.append(_doc_id_from_simvec(point["id"]))
            off = pg.get("next_offset")
            if not off:
                return scoped

    def diagnostics(self) -> dict[str, Any]:
        """Backend-neutral health snapshot for the local vector engine."""
        info: dict[str, Any] = {
            "backend": "simvec",
            "collection": self.collection,
            "root": str(self.root),
            "profile": self.profile.identity(),
            "profile_fingerprint": self.profile.fingerprint(),
            "tuning_fingerprint": self.profile.tuning_fingerprint(),
            "write_batch": self.write_batch,
            "max_points": self.max_points,
        }
        try:
            exists = self._engine.collection_exists(self.collection)
            info["collection_exists"] = exists
            if exists:
                info["points"] = int(self._engine.count_by_filter(self.collection, {}))
                # Whether the graph is published decides which search path serves reads
                info["has_index"] = bool(self._engine.has_index(self.collection))
                info["payload_indexes"] = sorted(
                    str(item.get("field"))
                    for item in self._engine.list_payload_indexes(self.collection)
                )
                try:
                    metrics = self._engine.collection_metrics(self.collection)
                except Exception:  # noqa: BLE001 — metrics are advisory; absence is not a failure
                    metrics = None
                if metrics:
                    info["metrics"] = metrics
            info["reachable"] = True
        except Exception as exc:  # noqa: BLE001 — diagnostics must not raise
            info["reachable"] = False
            info["error"] = str(exc)
        return info
