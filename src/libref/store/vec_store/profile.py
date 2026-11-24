"""Collection naming and profile drift governance."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any


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


INDEX_TIERS = ("hnsw", "flat")


DISTANCES = ("cosine", "euclid", "dot", "manhattan")


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
