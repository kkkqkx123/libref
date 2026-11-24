"""Vector metadata and collection capacity bookkeeping."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from libref.store.vec_store.profile import VectorProfile


@runtime_checkable
class MetaWriter(Protocol):
    """Metadata write handle a vector backend needs for its own bookkeeping.

    Satisfied by the embedded driver connection and by the backend-neutral
    metadata session, so collection governance never names a driver type.
    """

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any: ...

    def executemany(self, sql: str, seq: Iterable[tuple[Any, ...]]) -> Any: ...

    def commit(self) -> None: ...


class VectorCapacityError(RuntimeError):
    """A write would push the collection past its configured point ceiling.

    Deliberately a hard stop: the vector index is a derived store, so the source of
    truth is untouched and the batch can be replayed once capacity is raised.
    """


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
