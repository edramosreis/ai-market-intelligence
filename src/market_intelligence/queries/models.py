"""Evidence returned by market queries; Decimal values serialize as JSON strings."""

import base64
import binascii
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from market_intelligence.ingestion.models import TimeWindow, aligned, as_utc, parse_instant

RESOLUTIONS = (300, 900, 3600, 86400)


class QueryValidationError(ValueError):
    pass


class UnknownMarketError(LookupError):
    pass


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Market(Evidence):
    id: int
    source_code: str
    source_name: str
    source_product_id: str
    base_asset_code: str
    quote_asset_code: str
    canonical_interval_seconds: int = 300
    supported_intervals_seconds: tuple[int, ...] = RESOLUTIONS
    earliest_opened_at: datetime | None
    latest_opened_at: datetime | None


class Provenance(Evidence):
    first_ingested_at: datetime | None
    last_updated_at: datetime | None
    ingestion_run_count: int
    last_ingestion_run_id: UUID | None = None


class CandleEvidence(Evidence):
    opened_at: datetime
    ended_at: datetime
    interval_seconds: int
    status: Literal["complete", "incomplete"]
    expected_constituents: int
    actual_constituents: int
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    base_volume: Decimal | None
    provenance: Provenance


class MissingRange(Evidence):
    start: datetime
    end: datetime
    missing_buckets: int


class Coverage(Evidence):
    status: Literal["complete", "incomplete", "no_data"]
    expected_buckets: int
    actual_buckets: int
    missing_buckets: int
    ratio: Decimal
    observed_start: datetime | None
    observed_end: datetime | None
    missing_ranges: list[MissingRange]
    missing_range_count: int
    missing_ranges_truncated: bool


class WindowEvidence(Evidence):
    market: Market
    start: datetime
    end: datetime
    retrieved_at: datetime
    coverage: Coverage


class CandlePage(WindowEvidence):
    output_interval_seconds: int
    candles: list[CandleEvidence]
    next_cursor: str | None


class Summary(WindowEvidence):
    opening_price: Decimal | None
    closing_price: Decimal | None
    open_to_close_return_percent: Decimal | None
    high: Decimal | None
    low: Decimal | None
    base_volume: Decimal | None
    provenance: Provenance


class Latest(Evidence):
    market: Market
    status: Literal["available", "no_data"]
    retrieved_at: datetime
    candle: CandleEvidence | None
    age_seconds: Decimal | None
    stale: bool | None
    stale_after_seconds: int


def validate_window(start: datetime, end: datetime, resolution: int, max_days: int) -> TimeWindow:
    try:
        window = TimeWindow(start, end)
    except ValueError as error:
        raise QueryValidationError(
            "Use aware, five-minute-aligned start/end with start < end"
        ) from error
    if resolution not in RESOLUTIONS:
        raise QueryValidationError("Supported interval_seconds: 300, 900, 3600, 86400")
    if any(int(instant.timestamp()) % resolution for instant in (window.start, window.end)):
        raise QueryValidationError("Start and end must align to the output interval's UTC grid")
    if window.end - window.start > timedelta(days=max_days):
        raise QueryValidationError(f"Window exceeds the configured {max_days}-day limit")
    return window


def cursor_for(market_id: int, window: TimeWindow, resolution: int, after: datetime) -> str:
    data = [
        1,
        market_id,
        window.start.isoformat(),
        window.end.isoformat(),
        resolution,
        after.isoformat(),
    ]
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


def cursor_after(token: str, market_id: int, window: TimeWindow, resolution: int) -> datetime:
    try:
        if len(token) > 1024:
            raise ValueError
        data = json.loads(base64.b64decode(token, altchars=b"-_", validate=True))
        if not isinstance(data, list) or len(data) != 6:
            raise ValueError
        if data[:5] != [1, market_id, window.start.isoformat(), window.end.isoformat(), resolution]:
            raise ValueError
        if not isinstance(data[5], str):
            raise ValueError
        after = as_utc(parse_instant(data[5]))
        if (
            not aligned(after)
            or int(after.timestamp()) % resolution
            or not window.start <= after < window.end
        ):
            raise ValueError
        return after
    except (ValueError, TypeError, binascii.Error, UnicodeError) as error:
        raise QueryValidationError(
            "Invalid cursor or cursor does not match this market/window/interval"
        ) from error
