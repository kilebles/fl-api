"""UTC helpers: naive UTC for SQLite DateTime columns and ISO-8601 `Z` serialization for API models."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer


def utcnow() -> datetime:
    """Return current UTC timestamp without tzinfo (safe for SQLite DateTime columns and naive comparisons)."""
    return datetime.now(UTC).replace(tzinfo=None)


def parse_naive_utc(raw: str | None) -> datetime:
    """Parse an ISO-8601 string into naive UTC, falling back to `utcnow()`."""
    if not isinstance(raw, str) or not raw.strip():
        return utcnow()
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def _attach_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


UtcDatetime = Annotated[
    datetime,
    AfterValidator(_attach_utc),
    PlainSerializer(lambda dt: _attach_utc(dt).isoformat().replace("+00:00", "Z"), return_type=str, when_used="json"),
]
