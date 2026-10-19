"""Shared aware-UTC handling and explicit CLI/HTTP timestamp parsing."""

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Timestamps must include a UTC offset")
    return instant.astimezone(UTC)


def parse_instant(value: str) -> datetime:
    # CLI date-only shorthand is explicitly UTC; full timestamps require an offset.
    if len(value) == 10:
        value += "T00:00:00+00:00"
    return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
