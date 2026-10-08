"""Reader evidence separates funding fractions, context prices, and OI receipt coverage."""

import base64
import binascii
import json
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from market_intelligence.hyperliquid.models import INSTRUMENT_CODE, SOURCE_CODE, milliseconds, utc
from market_intelligence.queries.models import Evidence, QueryValidationError


class PerpetualInstrument(Evidence):
    source_code: Literal["hyperliquid"] = SOURCE_CODE
    code: Literal["BTC-PERP"] = INSTRUMENT_CODE
    source_coin: Literal["BTC"] = "BTC"
    base_asset: Literal["BTC"] = "BTC"
    denomination_asset: Literal["USDT"] = "USDT"
    collateral_asset: Literal["USDC"] = "USDC"
    settlement_asset: Literal["USDC"] = "USDC"
    instrument_type: Literal["linear_perpetual"] = "linear_perpetual"
    contract_size_base: Decimal = Decimal(1)


class PerpetualResult(Evidence):
    source_name: Literal["Hyperliquid"] = "Hyperliquid"
    instrument: PerpetualInstrument
    retrieved_at: datetime


class FundingProvenance(Evidence):
    first_ingested_at: datetime
    last_updated_at: datetime
    last_ingestion_run_id: UUID
    meaning: Literal["materialization_times_not_historical_availability"] = (
        "materialization_times_not_historical_availability"
    )


class FundingObservation(Evidence):
    event_at: datetime
    settlement_hour: datetime
    funding_rate: Decimal
    premium: Decimal
    provenance: FundingProvenance


class FundingResult(PerpetualResult):
    funding_rate_unit: Literal["fraction_per_hour"] = "fraction_per_hour"
    premium_unit: Literal["fraction"] = "fraction"
    positive_funding_direction: Literal["longs_pay_shorts"] = "longs_pay_shorts"
    value_policy: Literal["current_values_with_ingestion_provenance"] = (
        "current_values_with_ingestion_provenance"
    )


class FundingLatest(FundingResult):
    status: Literal["fresh", "stale", "no_data"]
    event: FundingObservation | None
    age_seconds: Decimal | None
    stale_after_seconds: int


class MissingFundingRange(Evidence):
    start: datetime
    end: datetime
    missing_hours: int


class FundingCoverage(Evidence):
    basis: Literal["stored_events_on_derived_UTC_hour_grid"] = (
        "stored_events_on_derived_UTC_hour_grid"
    )
    status: Literal["complete", "incomplete", "no_data"]
    expected_hours: int
    observed_hours: int
    missing_hours: int
    first_event_at: datetime | None
    last_event_at: datetime | None
    missing_ranges: list[MissingFundingRange]
    missing_range_count: int
    missing_ranges_truncated: bool


class FundingPage(FundingResult):
    start: datetime
    end: datetime
    coverage: FundingCoverage
    events: list[FundingObservation]
    next_cursor: str | None


class FundingSummary(FundingResult):
    start: datetime
    end: datetime
    coverage: FundingCoverage
    rate_sum: Decimal | None
    rate_sum_percent: Decimal | None
    mean_rate: Decimal | None
    definition: Literal[
        "arithmetic sum of settled hourly fractions; no compounding or position path"
    ] = "arithmetic sum of settled hourly fractions; no compounding or position path"
    mean_rounding: Literal["18 decimal places, half even"] = "18 decimal places, half even"


class OpenInterestObservation(Evidence):
    snapshot_id: UUID
    fetch_started_at: datetime
    received_at: datetime
    source_event_at: None = None
    open_interest_btc: Decimal
    mark_price_usdt: Decimal
    oracle_price_usdt: Decimal
    ingestion_run_id: UUID


class OpenInterestResult(PerpetualResult):
    quantity_unit: Literal["BTC"] = "BTC"
    price_unit: Literal["USDT"] = "USDT"
    time_basis: Literal["local_fetch_and_receipt_times"] = "local_fetch_and_receipt_times"
    value_policy: Literal["immutable_observed_receipts"] = "immutable_observed_receipts"
    historical_completeness: Literal["not_established"] = "not_established"


class OpenInterestLatest(OpenInterestResult):
    status: Literal["fresh", "stale", "no_data"]
    snapshot: OpenInterestObservation | None
    age_seconds: Decimal | None
    stale_after_seconds: int


class OpenInterestCoverage(Evidence):
    basis: Literal["stored_receipts_in_requested_window"] = "stored_receipts_in_requested_window"
    observed_snapshots: int
    first_received_at: datetime | None
    last_received_at: datetime | None
    historical_completeness: Literal["not_established"] = "not_established"


class OpenInterestPage(OpenInterestResult):
    start: datetime
    end: datetime
    coverage: OpenInterestCoverage
    snapshots: list[OpenInterestObservation]
    next_cursor: str | None


def cursor_for(
    kind: str, start: datetime, end: datetime, after: datetime, identity: UUID | None = None
) -> str:
    values = [
        1,
        SOURCE_CODE,
        INSTRUMENT_CODE,
        kind,
        start.isoformat(),
        end.isoformat(),
        after.isoformat(),
        str(identity) if identity else None,
    ]
    return (
        base64.urlsafe_b64encode(json.dumps(values, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def cursor_after(
    cursor: str | None, kind: str, start: datetime, end: datetime
) -> tuple[datetime, UUID | None] | None:
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
            raise ValueError
        values = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(values, list)
            or len(values) != 8
            or values[:6]
            != [1, SOURCE_CODE, INSTRUMENT_CODE, kind, start.isoformat(), end.isoformat()]
        ):
            raise ValueError
        if not isinstance(values[6], str) or (
            kind == "open_interest" and not isinstance(values[7], str)
        ):
            raise ValueError
        after = utc(datetime.fromisoformat(values[6]))
        identity = UUID(values[7]) if kind == "open_interest" else None
        if kind == "funding":
            milliseconds(after)
        if not start <= after < end or cursor_for(kind, start, end, after, identity) != cursor:
            raise ValueError
        return after, identity
    except ValueError, TypeError, binascii.Error, OverflowError, RecursionError:
        raise QueryValidationError(
            "Invalid Hyperliquid continuation for this dataset/window"
        ) from None
