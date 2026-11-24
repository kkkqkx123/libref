"""Local simvec engine binding and store implementation."""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from libref.store.vec_store.helpers import (
    _WRITE_BATCH,
    VectorStore,
    _doc_id_from_simvec,
    _normalize_tenant_id,
    _simvec_id,
    _split_point,
    _tenant_scoped,
)
from libref.store.vec_store.meta import (
    MetaWriter,
    _check_capacity,
    _read_profile,
    _write_vec_meta,
)
from libref.store.vec_store.profile import VectorProfile, _needs_recreate


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


_MEM_SIMVEC_ROOT: Path | None = None
