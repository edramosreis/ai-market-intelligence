"""Stored Treasury evidence, native dates/units, and bound date-keyset cursors."""

import base64
import binascii
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from market_intelligence.queries.models import Evidence, QueryValidationError
from market_intelligence.treasury.models import (
    DATASET_CODE,
    SOURCE_CODE,
    TreasuryMissingReason,
    TreasuryTenor,
)


class TreasuryProvenance(Evidence):
    first_ingested_at: datetime
    last_updated_at: datetime
    last_ingestion_run_id: UUID


class TreasuryRateEvidence(Evidence):
    tenor: TreasuryTenor
    yield_percent: Decimal | None
    missing_reason: TreasuryMissingReason | Literal["not_stored"] | None
    provenance: TreasuryProvenance | None


class TreasurySpread(Evidence):
    definition: Literal["10Y minus 2Y"] = "10Y minus 2Y"
    status: Literal["available", "unavailable"]
    percentage_points: Decimal | None
    basis_points: Decimal | None
    missing_inputs: list[TreasuryTenor]


class TreasuryMonthRead(Evidence):
    run_id: UUID
    start: date
    end: date
    finished_at: datetime
    received_dates: int
    retained_dates: int
    # Audit time is a monthly fetch time, not each fact's verification/release timestamp.
    meaning: Literal["validated_monthly_feed_read"] = "validated_monthly_feed_read"


class TreasuryCurveEvidence(Evidence):
    observed_on: date
    status: Literal["stored", "incomplete_stored_curve", "no_data"]
    stored_rates: int
    available_rates: int
    rates: list[TreasuryRateEvidence]
    spread: TreasurySpread
    latest_month_read: TreasuryMonthRead | None


class TreasuryCoverage(Evidence):
    basis: Literal["stored_source_dates"] = "stored_source_dates"
    publication_calendar_completeness: Literal["not_established"] = "not_established"
    status: Literal["observations_stored", "no_data"]
    observed_dates: int
    stored_rates: int
    available_rates: int
    source_null: int
    field_absent: int
    first_observed_on: date | None
    last_observed_on: date | None


class TreasuryCurveResult(Evidence):
    source_code: Literal["us_treasury"] = SOURCE_CODE
    source_name: Literal["US Treasury"] = "US Treasury"
    dataset_code: Literal["daily_nominal_par_yield_curve"] = DATASET_CODE
    yield_unit: Literal["percent"] = "percent"
    value_policy: Literal["current_values_with_ingestion_provenance"] = (
        "current_values_with_ingestion_provenance"
    )
    retrieved_at: datetime
    curve: TreasuryCurveEvidence


class TreasuryCurvePage(Evidence):
    source_code: Literal["us_treasury"] = SOURCE_CODE
    source_name: Literal["US Treasury"] = "US Treasury"
    dataset_code: Literal["daily_nominal_par_yield_curve"] = DATASET_CODE
    yield_unit: Literal["percent"] = "percent"
    value_policy: Literal["current_values_with_ingestion_provenance"] = (
        "current_values_with_ingestion_provenance"
    )
    retrieved_at: datetime
    start: date
    end: date
    coverage: TreasuryCoverage
    curves: list[TreasuryCurveEvidence]
    next_cursor: str | None


def treasury_cursor(start: date, end: date, after: date) -> str:
    payload = [1, SOURCE_CODE, DATASET_CODE, start.isoformat(), end.isoformat(), after.isoformat()]
    return (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def treasury_cursor_after(cursor: str | None, start: date, end: date) -> date | None:
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
            raise ValueError
        payload = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(payload, list)
            or len(payload) != 6
            or type(payload[0]) is not int
            or payload[:5] != [1, SOURCE_CODE, DATASET_CODE, start.isoformat(), end.isoformat()]
            or not isinstance(payload[-1], str)
        ):
            raise ValueError
        after = date.fromisoformat(payload[-1])
        if payload[-1] != after.isoformat() or not start <= after < end:
            raise ValueError
        return after
    except ValueError, TypeError, binascii.Error, UnicodeError:
        raise QueryValidationError("Invalid Treasury cursor or mismatched date window") from None
