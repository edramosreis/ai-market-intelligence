"""Native monthly reader evidence and cursors bound to their query identity."""

import base64
import binascii
import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from market_intelligence.macro.models import MacroSeries
from market_intelligence.queries.models import Evidence, QueryValidationError


class UnknownMacroSeriesError(LookupError):
    pass


class MacroSeriesEvidence(Evidence):
    series_id: MacroSeries
    source_code: Literal["bls", "federal_reserve_board"]
    source_name: str
    source_url: str
    title: str
    unit: Literal["index_1982_84_100", "percent", "percent_per_annum"]
    seasonal_adjustment: Literal["seasonally_adjusted", "not_seasonally_adjusted"]
    earliest_native_month: date
    stored_months: int
    first_stored_month: date | None
    last_stored_month: date | None
    source_notice: str | None


class MacroFootnoteEvidence(Evidence):
    code: str
    text: str


class MacroProvenance(Evidence):
    first_materialized_at: datetime
    materialized_at: datetime
    content_receipt_id: UUID


class MacroObservationEvidence(Evidence):
    month: date
    native_period: str
    version_number: int
    status: Literal["available", "source_missing"]
    value: Decimal | None
    missing_reason: Literal["source_dash"] | None
    footnotes: list[MacroFootnoteEvidence]
    provenance: MacroProvenance


class MacroReceiptEvidence(Evidence):
    run_id: UUID
    start: date
    end: date
    fetch_started_at: datetime
    received_at: datetime
    access_date: date
    finished_at: datetime
    received_periods: int
    retained_periods: int
    prepared_text: str | None
    source_annotations: list[tuple[str, str]]
    source_messages: list[str]
    latest_hints: list[tuple[MacroSeries, date]]
    meaning: Literal["receipt_that_materialized_version_content"] = (
        "receipt_that_materialized_version_content"
    )
    per_observation_publication_time: Literal["not_established"] = "not_established"


class MacroMissingRange(Evidence):
    start: date
    end: date
    months: int


class MacroCoverage(Evidence):
    basis: Literal["native_month_grid"] = "native_month_grid"
    publication_calendar_completeness: Literal["not_established"] = "not_established"
    status: Literal["complete", "incomplete", "no_data"]
    expected_months: int
    stored_months: int
    available_values: int
    source_missing_values: int
    not_stored_months: int
    first_stored_month: date | None
    last_stored_month: date | None
    missing_ranges: list[MacroMissingRange]
    missing_range_count: int
    missing_ranges_truncated: bool


class MacroResult(Evidence):
    series: MacroSeriesEvidence
    retrieved_at: datetime
    historical_release_vintages: Literal["not_established"] = "not_established"
    version_history_basis: Literal["content_changes_observed_locally"] = (
        "content_changes_observed_locally"
    )
    receipts: list[MacroReceiptEvidence]


class MacroObservationPage(MacroResult):
    value_policy: Literal["current_stored_values"] = "current_stored_values"
    start: date
    end: date
    coverage: MacroCoverage
    observations: list[MacroObservationEvidence]
    next_cursor: str | None


class MacroLatest(MacroResult):
    basis: Literal["latest_stored_completed_month"] = "latest_stored_completed_month"
    status: Literal["stored", "no_data"]
    observation: MacroObservationEvidence | None
    latest_completed_month: date
    months_behind_latest_completed: int | None
    # A period lag is not collection age or a verified publication-calendar delay.
    publication_delay: Literal["not_established"] = "not_established"


class MacroVersionPage(MacroResult):
    month: date
    current_version_number: int | None
    observed_versions: int
    versions: list[MacroObservationEvidence]
    next_cursor: str | None


def month_date(value: str) -> date:
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-01", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise QueryValidationError("Use monthly DATE labels as YYYY-MM-01") from None


def month_number(value: date) -> int:
    return value.year * 12 + value.month - 1


def month_at(number: int) -> date:
    year, month = divmod(number, 12)
    return date(year, month + 1, 1)


def _encode(payload: list[object]) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _decode(cursor: str, prefix: list[object]) -> object:
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
            raise ValueError
        payload = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(payload, list)
            or len(payload) != len(prefix) + 1
            or type(payload[0]) is not int
            or payload[:-1] != prefix
        ):
            raise ValueError
        return payload[-1]
    except ValueError, TypeError, binascii.Error, UnicodeError, RecursionError:
        raise QueryValidationError("Invalid macro cursor or mismatched query") from None


def observation_cursor(series: MacroSeries, start: date, end: date, after: date) -> str:
    return _encode(
        [1, "macro_current", series.value, start.isoformat(), end.isoformat(), after.isoformat()]
    )


def observation_after(
    cursor: str | None, series: MacroSeries, start: date, end: date
) -> date | None:
    if cursor is None:
        return None
    after = _decode(cursor, [1, "macro_current", series.value, start.isoformat(), end.isoformat()])
    if not isinstance(after, str):
        raise QueryValidationError("Invalid macro month cursor")
    month = month_date(after)
    if not start <= month < end:
        raise QueryValidationError("Macro cursor month is outside the requested window")
    return month


def version_cursor(series: MacroSeries, month: date, after: int) -> str:
    return _encode([1, "macro_observed_versions", series.value, month.isoformat(), after])


def version_after(cursor: str | None, series: MacroSeries, month: date) -> int | None:
    if cursor is None:
        return None
    after = _decode(cursor, [1, "macro_observed_versions", series.value, month.isoformat()])
    if type(after) is not int or not 1 <= after <= 2147483647:
        raise QueryValidationError("Invalid macro version cursor")
    return after
