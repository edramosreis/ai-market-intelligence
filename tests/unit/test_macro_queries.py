"""Monthly grids, strict cursor identities and validation before database access."""

import base64
import json
from datetime import UTC, date, datetime
from typing import cast

import pytest
from sqlalchemy import Engine

from market_intelligence.config import ApiSettings
from market_intelligence.macro.models import MacroSeries
from market_intelligence.macro.queries import MacroQueries
from market_intelligence.macro.query_models import (
    UnknownMacroSeriesError,
    month_at,
    month_date,
    month_number,
    observation_after,
    observation_cursor,
    version_after,
    version_cursor,
)
from market_intelligence.queries.models import QueryValidationError

START, END = date(2024, 1, 1), date(2024, 6, 1)
NOW = datetime(2026, 10, 9, tzinfo=UTC)


def query(limit: int = 1200) -> MacroQueries:
    return MacroQueries(
        cast(Engine, object()),
        ApiSettings(_env_file=None, macro_max_window_months=limit),  # type: ignore[call-arg]
        now=lambda: NOW,
    )


@pytest.mark.parametrize(
    "value",
    ["2024-1-01", "2024-01-02", "2024-13-01", "2024-01", "20240101", "2024-01-01T00:00:00Z"],
)
def test_strict_month_labels(value: str) -> None:
    with pytest.raises(QueryValidationError):
        month_date(value)


def test_months_and_year_rollover() -> None:
    assert month_date("2024-02-01") == date(2024, 2, 1)
    assert month_at(month_number(date(2023, 12, 1)) + 1) == START
    assert month_at(month_number(date(1947, 1, 1))) == date(1947, 1, 1)


def test_both_cursor_roundtrips_and_null() -> None:
    cursor = observation_cursor(MacroSeries.CPI, START, END, date(2024, 2, 1))
    assert observation_after(cursor, MacroSeries.CPI, START, END) == date(2024, 2, 1)
    assert observation_after(None, MacroSeries.CPI, START, END) is None
    token = version_cursor(MacroSeries.CPI, START, 2)
    assert version_after(token, MacroSeries.CPI, START) == 2
    assert version_after(None, MacroSeries.CPI, START) is None


@pytest.mark.parametrize(
    "kind",
    [
        "series",
        "window",
        "outside",
        "unaligned",
        "version-kind",
        "bool-format",
        "bad-base64",
        "oversized",
    ],
)
def test_month_cursor_rejects_other_queries_and_malformed_tokens(kind: str) -> None:
    payload: list[object] = [
        1,
        "macro_current",
        MacroSeries.CPI.value,
        START.isoformat(),
        END.isoformat(),
        "2024-02-01",
    ]
    if kind == "series":
        payload[2] = MacroSeries.UNEMPLOYMENT.value
    elif kind == "window":
        payload[4] = "2024-07-01"
    elif kind == "outside":
        payload[-1] = END.isoformat()
    elif kind == "unaligned":
        payload[-1] = "2024-02-02"
    elif kind == "version-kind":
        payload[1] = "macro_observed_versions"
    elif kind == "bool-format":
        payload[0] = True
    token = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    if kind == "bad-base64":
        token = "!bad!"
    elif kind == "oversized":
        token = "a" * 1025
    with pytest.raises(QueryValidationError):
        observation_after(token, MacroSeries.CPI, START, END)


@pytest.mark.parametrize("after", [True, 0, -1, "1", 1.5, 2147483648])
def test_version_cursor_requires_positive_bounded_integer(after: object) -> None:
    token = base64.urlsafe_b64encode(
        json.dumps(
            [1, "macro_observed_versions", MacroSeries.CPI.value, START.isoformat(), after]
        ).encode()
    ).decode()
    with pytest.raises(QueryValidationError):
        version_after(token, MacroSeries.CPI, START)


def test_version_cursor_binds_month_series_and_kind() -> None:
    token = version_cursor(MacroSeries.CPI, START, 1)
    for series, month in [(MacroSeries.CPI, date(2024, 2, 1)), (MacroSeries.UNEMPLOYMENT, START)]:
        with pytest.raises(QueryValidationError):
            version_after(token, series, month)
    with pytest.raises(QueryValidationError):
        version_after(
            observation_cursor(MacroSeries.CPI, START, END, START), MacroSeries.CPI, START
        )


@pytest.mark.parametrize(
    "start,end",
    [
        (date(1946, 12, 1), START),
        (date(2024, 1, 2), END),
        (START, date(2024, 6, 2)),
        (END, START),
        (START, START),
        (START, date(2026, 11, 1)),
        (datetime(2024, 1, 1, tzinfo=UTC), END),
    ],
)
def test_invalid_windows_fail_before_engine_access(start: date, end: date) -> None:
    with pytest.raises(QueryValidationError):
        query().observations(MacroSeries.CPI, start, end)


def test_series_specific_history_and_configured_width() -> None:
    with pytest.raises(QueryValidationError):
        query().observations(MacroSeries.UNEMPLOYMENT, date(1947, 1, 1), START)
    with pytest.raises(QueryValidationError):
        query().observations(MacroSeries.FED_FUNDS, date(1954, 6, 1), START)
    with pytest.raises(QueryValidationError):
        query(4).observations(MacroSeries.CPI, START, END)
    assert query().validate_bounds(MacroSeries.CPI, date(1947, 1, 1), date(2026, 10, 1)) == NOW


@pytest.mark.parametrize("limit", [True, 0, 101])
def test_invalid_limits_fail_before_engine_access(limit: int) -> None:
    with pytest.raises(QueryValidationError):
        query().observations(MacroSeries.CPI, START, END, limit)


def test_unknown_series_fails_before_engine_access() -> None:
    with pytest.raises(UnknownMacroSeriesError):
        query().latest("not-a-native-series")


def test_gap_ranges_distinguish_dash_from_absent_and_coalesce() -> None:
    result = query().coverage(
        START,
        END,
        [
            {"observation_month": START, "value": 1},
            {"observation_month": date(2024, 3, 1), "value": None},
        ],
    )
    assert result.status == "incomplete"
    assert (
        result.expected_months,
        result.stored_months,
        result.available_values,
        result.source_missing_values,
        result.not_stored_months,
    ) == (5, 2, 1, 1, 3)
    assert [(gap.start, gap.end, gap.months) for gap in result.missing_ranges] == [
        (date(2024, 2, 1), date(2024, 3, 1), 1),
        (date(2024, 4, 1), END, 2),
    ]
    assert result.publication_calendar_completeness == "not_established"


def test_gap_range_truncation_keeps_whole_window_counts() -> None:
    start = date(1947, 1, 1)
    rows = [
        {"observation_month": month_at(month_number(start) + offset), "value": 1}
        for offset in range(0, 220, 2)
    ]
    result = query().coverage(start, month_at(month_number(start) + 220), rows)
    assert result.not_stored_months == result.missing_range_count == 110
    assert len(result.missing_ranges) == 100 and result.missing_ranges_truncated


def test_explicit_dash_can_have_complete_month_grid() -> None:
    result = query().coverage(
        START, date(2024, 2, 1), [{"observation_month": START, "value": None}]
    )
    assert result.status == "complete" and result.source_missing_values == 1
    assert result.not_stored_months == 0
