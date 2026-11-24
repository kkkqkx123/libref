"""Qdrant vector store implementation."""

from __future__ import annotations

from typing import Any, cast

from libref.store.vec_store.helpers import (
    _WRITE_BATCH,
    VectorStore,
    _normalize_tenant_id,
    _split_point,
    _tenant_scoped,
)
from libref.store.vec_store.meta import (
    MetaWriter,
    _check_capacity,
    _read_profile,
    _write_vec_meta,
)
from libref.store.vec_store.profile import VectorProfile, _needs_recreate, _needs_retune


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
