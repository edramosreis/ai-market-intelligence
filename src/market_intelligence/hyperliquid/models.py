"""Exact source event times, signed funding fractions, and local OI receipt times."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

SOURCE_CODE: Final = "hyperliquid"
INSTRUMENT_CODE: Final = "BTC-PERP"
COIN: Final = "BTC"
HISTORY_START: Final = datetime(2024, 1, 1, tzinfo=UTC)
EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class HyperliquidErrorCode(StrEnum):
    INVALID_PAYLOAD = "invalid_payload"
    HTTP_ERROR = "http_error"
    RETRY_EXHAUSTED = "retry_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    DATABASE_ERROR = "database_error"
    CONCURRENT_JOB = "concurrent_job"
    INTERRUPTED = "interrupted"
    INTERNAL_ERROR = "internal_error"


class HyperliquidError(Exception):
    def __init__(self, code: HyperliquidErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


def utc(instant: datetime) -> datetime:
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("Hyperliquid instants must be aware")
    return instant.astimezone(UTC)


def milliseconds(instant: datetime) -> int:
    value = utc(instant)
    if value.microsecond % 1000:
        raise ValueError("Source bounds/events require millisecond precision")
    return (value - EPOCH) // timedelta(milliseconds=1)


def hour(instant: datetime) -> datetime:
    return utc(instant).replace(minute=0, second=0, microsecond=0)


def numeric(value: Decimal, *, nonnegative: bool = False, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("Expected finite Decimal")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -18 or value.adjusted() >= 20:
        raise ValueError("Value exceeds numeric(38,18)")
    if (nonnegative and value < 0) or (positive and value <= 0):
        raise ValueError("Invalid quantity/price sign")


@dataclass(frozen=True)
class FundingWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start, end = utc(self.start), utc(self.end)
        milliseconds(start)
        milliseconds(end)
        if start < HISTORY_START or start >= end or end - start > timedelta(days=31):
            raise ValueError("Funding source windows start in 2024 and span at most 31 days")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True)
class FundingEvent:
    event_at: datetime
    funding_rate: Decimal
    premium: Decimal

    def __post_init__(self) -> None:
        event_at = utc(self.event_at)
        milliseconds(event_at)
        if event_at < HISTORY_START:
            raise ValueError("Funding event predates the selected history")
        numeric(self.funding_rate)
        numeric(self.premium)
        object.__setattr__(self, "event_at", event_at)

    @property
    def settlement_hour(self) -> datetime:
        """Derived UTC hour containing the event; never replaces its exact timestamp."""
        return hour(self.event_at)


@dataclass(frozen=True)
class OpenInterestSnapshot:
    snapshot_id: UUID
    fetch_started_at: datetime
    received_at: datetime
    open_interest_btc: Decimal
    mark_price_usdt: Decimal
    oracle_price_usdt: Decimal

    def __post_init__(self) -> None:
        start, end = utc(self.fetch_started_at), utc(self.received_at)
        if not isinstance(self.snapshot_id, UUID) or start > end:
            raise ValueError("Invalid snapshot identity/receipt interval")
        numeric(self.open_interest_btc, nonnegative=True)
        numeric(self.mark_price_usdt, positive=True)
        numeric(self.oracle_price_usdt, positive=True)
        object.__setattr__(self, "fetch_started_at", start)
        object.__setattr__(self, "received_at", end)


def funding_windows(start: datetime, end: datetime) -> list[FundingWindow]:
    start, end = utc(start), utc(end)
    milliseconds(end)
    if start != hour(start) or start < HISTORY_START or start >= end:
        raise ValueError("Funding ingestion starts on a UTC hour from 2024, with start < end")
    result = []
    while start < end:
        boundary = (
            datetime(start.year + 1, 1, 1, tzinfo=UTC)
            if start.month == 12
            else datetime(start.year, start.month + 1, 1, tzinfo=UTC)
        )
        window = FundingWindow(start, min(boundary, end))
        result.append(window)
        start = window.end
    return result


def expected_hours(window: FundingWindow) -> int:
    if window.start != hour(window.start):
        raise ValueError("Ingestion windows start on a UTC hour")
    return ((milliseconds(window.end) - milliseconds(window.start)) + 3_599_999) // 3_600_000


class HyperliquidIngestionError(HyperliquidError):
    def __init__(self, code: HyperliquidErrorCode, operation: str) -> None:
        super().__init__(code)
        self.operation = operation
        self.window: FundingWindow | None = None
        self.run_id: UUID | None = None
        self.audit_recorded = False


@dataclass(frozen=True)
class FundingReport:
    run_id: UUID
    window: FundingWindow
    expected_hours: int
    received: int
    inserted: int
    updated: int
    unchanged: int
    retained: int
    skipped: bool = False

    @property
    def missing_hours(self) -> int:
        return self.expected_hours - self.received - self.retained
