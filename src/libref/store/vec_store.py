"""Vector index wrapper, a derived index over the SQLite source of truth.

Vector point ids map one-to-one to document row ids; the payload stores only filter
keys and body text is fetched back from SQLite. Callers are the orchestration layer;
the API layer never touches this directly. Two backends share one surface:
the remote Qdrant service and the local crates/simvec engine.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from qdrant_client import QdrantClient, models


def collection_for(base: str, db_path: str | os.PathLike[str]) -> str:
    """Derive the collection name from the database file identity; different db files are naturally isolated, in-memory dbs keep the base name."""
    raw = os.fspath(db_path)
    if raw == ":memory:":
        return base
    digest = hashlib.sha1(os.path.abspath(raw).encode("utf-8")).hexdigest()[:12]
    return f"{base}_{digest}"


@runtime_checkable
class VectorStore(Protocol):
    """Six-method surface every vector backend implements; callers stay duck-typed."""

    collection: str

    def ensure_collection(
        self, conn: sqlite3.Connection, model: str, dim: int
    ) -> None: ...

    def upsert_batch(
        self, points: list[tuple[int, int, int, list[float]]], *, wait: bool = True
    ) -> None: ...

    def delete_docs(self, doc_ids: list[int]) -> None: ...

    def delete_version(self, version_id: int) -> None: ...

    def search(
        self, query_vec: list[float], *, k: int, version_id: int
    ) -> list[tuple[int, float]]: ...

    def count(self, version_id: int) -> int: ...


def _read_vec_meta(conn: sqlite3.Connection) -> tuple[str | None, int | None]:
    rows = dict(
        conn.execute(
            "SELECT key, value FROM schema_meta WHERE key IN ('vec_model','vec_dim')"
        )
    )
    model = rows.get("vec_model")
    dim = int(rows["vec_dim"]) if "vec_dim" in rows else None
    return model, dim


def _write_vec_meta(conn: sqlite3.Connection, model: str, dim: int) -> None:
    conn.executemany(
        "INSERT INTO schema_meta(key, value) VALUES(?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (("vec_model", model), ("vec_dim", str(dim))),
    )
    conn.commit()


class QdrantVecStore(VectorStore):
    """Remote backend: single collection with payload-version filtering; distance is cosine."""

    def __init__(
        self,
        url: str,
        collection: str,
        *,
        timeout: float = 5.0,
        client: Any | None = None,
    ) -> None:
        self.collection = collection
        self.client = (
            client if client is not None else QdrantClient(url=url, timeout=timeout)
        )

    # ── Collection governance ──

    def ensure_collection(self, conn: sqlite3.Connection, model: str, dim: int) -> None:
        """Create or verify the collection and register model metadata.

        When the collection does not exist, create it with the given dimension plus
        filter indexes. If the model name, dimension or records mismatch, or the
        collection shape is unrecognizable, delete and recreate it and update the
        records. When records match, this is an idempotent no-op.
        """
        old_model, old_dim = _read_vec_meta(conn)
        if self.client.collection_exists(self.collection):
            cur_dim = self._collection_dim()
            if cur_dim != dim or old_dim != dim or old_model != model:
                self.client.delete_collection(self.collection)
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=dim, distance=models.Distance.COSINE
                ),
            )
            self._create_payload_indexes()
        _write_vec_meta(conn, model, dim)

    def _collection_dim(self) -> int | None:
        info = self.client.get_collection(self.collection)
        vectors = info.config.params.vectors
        if isinstance(vectors, models.VectorParams):
            return vectors.size
        return None

    def _create_payload_indexes(self) -> None:
        # Build indexes before loading data; version_id is the filter key, and the
        # integer tenant optimization keeps equal values linked
        self.client.create_payload_index(
            collection_name=self.collection,
            field_name="version_id",
            field_schema=models.IntegerIndexParams(
                type=models.IntegerIndexType.INTEGER, is_principal=True
            ),
        )
        self.client.create_payload_index(
            collection_name=self.collection,
            field_name="library_id",
            field_schema=models.PayloadSchemaType.INTEGER,
        )

    # ── Write ──

    def upsert_batch(
        self,
        points: list[tuple[int, int, int, list[float]]],
        *,
        wait: bool = True,
    ) -> None:
        """Batch-write document, library and version ids with vectors, idempotent overwrite."""
        if not points:
            return
        self.client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=doc_id,
                    vector=embedding,
                    payload={"library_id": library_id, "version_id": version_id},
                )
                for doc_id, library_id, version_id, embedding in points
            ],
            wait=wait,
        )

    def delete_docs(self, doc_ids: list[int]) -> None:
        """Delete vector points for the given documents, cleaning up old points on single-page re-chunking."""
        if not doc_ids:
            return
        self.client.delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=doc_ids),
        )

    def delete_version(self, version_id: int) -> None:
        """Delete all vector points of a version, on re-crawl of that version."""
        self.client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=self._version_filter(version_id)
            ),
        )

    # ── Search and reconciliation ──

    def search(
        self, query_vec: list[float], *, k: int, version_id: int
    ) -> list[tuple[int, float]]:
        """Nearest-neighbor search filtered by version; returns document ids and cosine similarity in descending similarity order."""
        res = self.client.query_points(
            collection_name=self.collection,
            query=query_vec,
            query_filter=self._version_filter(version_id),
            limit=k,
            with_payload=False,
        )
        return [(p.id, p.score) for p in res.points]

    def count(self, version_id: int) -> int:
        """Number of vector points in a version, for reconciliation against the documents table count."""
        return self.client.count(
            collection_name=self.collection,
            count_filter=self._version_filter(version_id),
            exact=True,
        ).count

    @staticmethod
    def _version_filter(version_id: int) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="version_id", match=models.MatchValue(value=version_id)
                )
            ]
        )


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


def _import_simvec_binding(lib: str | None = None):
    """Import the ctypes binding scoped to this loader; explicit path beats environment beats default discovery."""
    import importlib

    if lib is not None and os.environ.get("SIMVEC_LIB") != lib:
        os.environ["SIMVEC_LIB"] = lib
    bindings = str(_simvec_bindings_dir())
    if bindings not in sys.path:
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


_ENGINES: dict[str, Any] = {}


def simvec_available(lib: str | None = None) -> bool:
    """Whether the native simvec binding imports; used for test skip decisions."""
    try:
        _import_simvec_binding(lib)
    except SimvecLoadError:
        return False
    return True


def _open_simvec_engine(root: Path, lib: str | None = None):
    key = os.path.abspath(os.fspath(root))
    engine = _ENGINES.get(key)
    if engine is None:
        os.makedirs(key, exist_ok=True)
        engine = _import_simvec_binding(lib).Engine(key)
        _ENGINES[key] = engine
    return engine


def close_simvec_engines() -> None:
    """Release cached engines; used by tests and process teardown."""
    while _ENGINES:
        _, engine = _ENGINES.popitem()
        with contextlib.suppress(Exception):
            engine.close()


def simvec_root_for(db_path, simvec_dir=None) -> Path:
    """Derive the engine root from the database file; explicit directory wins, memory databases get an ephemeral root."""
    if simvec_dir is not None:
        return Path(simvec_dir)
    raw = os.fspath(db_path)
    if raw == ":memory:":
        return Path(tempfile.mkdtemp(prefix="libref-simvec-"))
    return Path(os.path.abspath(raw) + ".simvec")


def _simvec_version_filter(version_id: int) -> dict[str, Any]:
    # Match values cross the boundary as strings; numeric payloads compare by decimal form.
    return {
        "must": [
            {"field": "version_id", "condition": {"Match": {"value": str(version_id)}}}
        ]
    }


class SimvecVecStore(VectorStore):
    """Local backend: single collection with payload-version filtering; distance is cosine."""

    def __init__(
        self, root: str | Path, collection: str, *, lib: str | None = None
    ) -> None:
        self.root = Path(root)
        self.collection = collection
        self._engine = _open_simvec_engine(self.root, lib)

    def ensure_collection(self, conn: sqlite3.Connection, model: str, dim: int) -> None:
        """Create or verify the collection and register model metadata, mirroring the remote rebuild semantics."""
        if not self._engine.collection_exists(self.collection):
            self._create_collection(dim)
        else:
            old_model, old_dim = _read_vec_meta(conn)
            try:
                cur = self._engine.collection_config(self.collection)
                cur_dim = cur.get("vector_size")
            except Exception:  # noqa: BLE001 — unreadable shape counts as mismatch, rebuilt below
                cur_dim = None
            if cur_dim != dim or old_dim != dim or old_model != model:
                self._engine.delete_collection(self.collection)
                self._create_collection(dim)
        _write_vec_meta(conn, model, dim)

    def _create_collection(self, dim: int) -> None:
        # Flat exact scan keeps recall exact and the config shape stable;
        # the HNSW tier stays a future knob, not a migration-time decision.
        self._engine.create_collection(
            self.collection,
            {"vector_size": dim, "distance": "Cosine", "index": "Flat"},
        )
        indexed = {
            field for field, _ in self._engine.list_payload_indexes(self.collection)
        }
        for field in ("version_id", "library_id"):
            if field not in indexed:
                self._engine.create_payload_index(self.collection, field, "integer")

    def upsert_batch(
        self,
        points: list[tuple[int, int, int, list[float]]],
        *,
        wait: bool = True,
    ) -> None:
        """Batch-write document, library and version ids with vectors, idempotent overwrite."""
        if not points:
            return
        self._engine.upsert_batch(
            self.collection,
            [
                {
                    "id": str(doc_id),
                    "vector": list(embedding),
                    "payload": {"library_id": library_id, "version_id": version_id},
                }
                for doc_id, library_id, version_id, embedding in points
            ],
        )

    def delete_docs(self, doc_ids: list[int]) -> None:
        """Delete vector points for the given documents, cleaning up old points on single-page re-chunking."""
        if not doc_ids:
            return
        self._engine.delete_batch(self.collection, [str(i) for i in doc_ids])

    def delete_version(self, version_id: int) -> None:
        """Delete all vector points of a version, on re-crawl of that version."""
        self._engine.delete_by_filter(
            self.collection, _simvec_version_filter(version_id)
        )

    def search(
        self, query_vec: list[float], *, k: int, version_id: int
    ) -> list[tuple[int, float]]:
        """Nearest-neighbor search filtered by version; returns document ids and cosine similarity in descending similarity order."""
        hits = self._engine.search_vector(
            self.collection,
            list(query_vec),
            k,
            query_filter=_simvec_version_filter(version_id),
            with_payload=False,
            with_vector=False,
        )
        return [(int(h["id"]), float(h["score"])) for h in hits]

    def count(self, version_id: int) -> int:
        """Number of vector points in a version, for reconciliation against the documents table count."""
        return int(
            self._engine.count_by_filter(
                self.collection, _simvec_version_filter(version_id)
            )
        )
