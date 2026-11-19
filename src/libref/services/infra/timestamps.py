"""UTC timestamp normalization shared by web routes and service read models."""

from __future__ import annotations

from datetime import UTC, datetime


def _moment(value: object) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def iso_utc(value: object) -> str | None:
    """Render an instant as ISO 8601 UTC; pass through values that are not times."""
    moment = _moment(value)
    if moment is not None:
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return value if isinstance(value, str) else None


def epoch_utc(value: str | None) -> float | None:
    """Parse an ISO 8601 instant into epoch seconds for storage-backed services."""
    moment = _moment(value)
    return moment.timestamp() if moment is not None else None
