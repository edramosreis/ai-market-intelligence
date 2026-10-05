"""Canonical candle validation, UTC windows, and safe operational results."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

INTERVAL_SECONDS = 300
INTERVAL = timedelta(seconds=INTERVAL_SECONDS)
DEFAULT_START = datetime(2020, 1, 1, tzinfo=UTC)


class ErrorCode(StrEnum):
    INVALID_PAYLOAD = "invalid_payload"
    INVALID_CANDLE = "invalid_candle"
    PRODUCT_MISMATCH = "product_mismatch"
    HTTP_ERROR = "http_error"
    RETRY_EXHAUSTED = "retry_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    DATABASE_ERROR = "database_error"
    INTERRUPTED = "interrupted"
    CONCURRENT_JOB = "concurrent_job"
    INTERNAL_ERROR = "internal_error"


class IngestionError(Exception):
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code
        self.window: TimeWindow | None = None
        self.run_id: UUID | None = None
        self.audit_recorded = False


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Timestamps must include a UTC offset")
    return instant.astimezone(UTC)


def aligned(instant: datetime) -> bool:
    instant = as_utc(instant)
    return instant.minute % 5 == 0 and instant.second == 0 and instant.microsecond == 0


def closed_cutoff(now: datetime) -> datetime:
    settled = as_utc(now) - timedelta(seconds=60)
    return settled.replace(minute=settled.minute - settled.minute % 5, second=0, microsecond=0)


def parse_instant(value: str) -> datetime:
    # CLI date-only shorthand is explicitly UTC; full timestamps require an offset.
    if len(value) == 10:
        value += "T00:00:00+00:00"
    return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", as_utc(self.start))
        object.__setattr__(self, "end", as_utc(self.end))
        if not aligned(self.start) or not aligned(self.end):
            raise ValueError("Windows must align to the UTC five-minute grid")
        if self.start >= self.end:
            raise ValueError("Window start must precede its end")

    @property
    def expected(self) -> int:
        return (self.end - self.start) // INTERVAL


def monthly_windows(window: TimeWindow) -> list[TimeWindow]:
    result = []
    start = window.start
    while start < window.end:
        if start.month == 12:
            boundary = datetime(start.year + 1, 1, 1, tzinfo=UTC)
        else:
            boundary = datetime(start.year, start.month + 1, 1, tzinfo=UTC)
        end = min(boundary, window.end)
        result.append(TimeWindow(start, end))
        start = end
    return result


@dataclass(frozen=True)
class Candle:
    opened_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal

    def __post_init__(self) -> None:
        try:
            instant = as_utc(self.opened_at)
            if not aligned(instant):
                raise ValueError("Unaligned candle")
            object.__setattr__(self, "opened_at", instant)
            for number in (self.open, self.high, self.low, self.close, self.base_volume):
                if not isinstance(number, Decimal) or not number.is_finite():
                    raise ValueError("Candle values must be finite Decimals")
                exponent = number.as_tuple().exponent
                if not isinstance(exponent, int) or exponent < -18 or number.adjusted() >= 20:
                    raise ValueError("Candle value exceeds numeric(38,18)")
            if min(self.open, self.high, self.low, self.close) <= 0 or self.base_volume < 0:
                raise ValueError("Invalid price or volume")
            if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
                raise ValueError("Invalid OHLC bounds")
        except ValueError, TypeError:
            raise IngestionError(ErrorCode.INVALID_CANDLE) from None


@dataclass(frozen=True)
class ChunkReport:
    run_id: UUID
    window: TimeWindow
    received: int
    inserted: int
    updated: int
    unchanged: int
    missing_buckets: int
    skipped: bool = False
