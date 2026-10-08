from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from market_intelligence.macro.models import (
    CATALOG,
    Footnote,
    MacroSeries,
    MonthlyObservation,
    MonthlyWindow,
    ProviderRead,
)


def test_native_catalog_units_and_labels() -> None:
    assert CATALOG[MacroSeries.CPI].unit == "index_1982_84_100"
    assert CATALOG[MacroSeries.UNEMPLOYMENT].unit == "percent"
    assert CATALOG[MacroSeries.FED_FUNDS].unit == "percent_per_annum"
    assert CATALOG[MacroSeries.FED_FUNDS].seasonal_adjustment == "not_seasonally_adjusted"
    row = MonthlyObservation(MacroSeries.FED_FUNDS, date(2024, 2, 1), "2024-02-29", Decimal("0"))
    local = datetime(2024, 3, 1, 1, tzinfo=timezone(timedelta(hours=1)))
    read = ProviderRead(MonthlyWindow(date(2024, 2, 1), date(2024, 3, 1)), (row,), local, local)
    assert read.received_at == datetime(2024, 3, 1, tzinfo=UTC)
    assert row.value == 0 and row.month != date.fromisoformat(row.native_period)


@pytest.mark.parametrize(
    "start,end",
    [
        (date(2024, 1, 2), date(2024, 2, 1)),
        (date(2024, 1, 1), date(2024, 2, 2)),
        (date(2024, 1, 1), date(2024, 1, 1)),
        (date(2024, 2, 1), date(2024, 1, 1)),
        (datetime(2024, 1, 1, tzinfo=UTC), date(2024, 2, 1)),
    ],
)
def test_window_rejects_non_month_bounds(start: date, end: date) -> None:
    with pytest.raises(ValueError):
        MonthlyWindow(start, end)


@pytest.mark.parametrize(
    "series,native,value,reason",
    [
        (MacroSeries.CPI, "2024-M01", Decimal("0"), None),
        (MacroSeries.CPI, "2024-M01", Decimal("NaN"), None),
        (MacroSeries.CPI, "2024-M01", Decimal("1e-19"), None),
        (MacroSeries.CPI, "2024-M01", Decimal("1e20"), None),
        (MacroSeries.UNEMPLOYMENT, "2024-M01", Decimal("100.1"), None),
        (MacroSeries.UNEMPLOYMENT, "2024-M01", Decimal("-0.1"), None),
        (MacroSeries.UNEMPLOYMENT, "2024-M02", Decimal("0"), None),
        (MacroSeries.UNEMPLOYMENT, "2024-M01", None, None),
        (MacroSeries.UNEMPLOYMENT, "2024-M01", Decimal("0"), "source_dash"),
        (MacroSeries.FED_FUNDS, "2024-01-30", Decimal("1"), None),
        (MacroSeries.FED_FUNDS, "2024-01-31", None, "source_dash"),
        (MacroSeries.FED_FUNDS, "2024-01-31", Decimal("-9999"), None),
    ],
)
def test_invalid_native_observations(
    series: MacroSeries,
    native: str,
    value: Decimal | None,
    reason: str | None,
) -> None:
    with pytest.raises(ValueError):
        MonthlyObservation(series, date(2024, 1, 1), native, value, reason)


def test_dash_is_distinct_from_zero_and_absence() -> None:
    missing = MonthlyObservation(
        MacroSeries.UNEMPLOYMENT,
        date(2024, 1, 1),
        "2024-M01",
        None,
        "source_dash",
        (Footnote("X", "Synthetic unavailable period"),),
    )
    zero = MonthlyObservation(MacroSeries.UNEMPLOYMENT, date(2024, 2, 1), "2024-M02", Decimal("0"))
    assert missing.value is None and missing.missing_reason == "source_dash"
    assert zero.value == 0 and zero.missing_reason is None
    with pytest.raises(ValueError):
        MonthlyObservation(MacroSeries.UNEMPLOYMENT, date(1947, 12, 1), "1947-M12", Decimal("3"))


def test_receipt_rejects_naive_reversed_and_duplicate_evidence() -> None:
    window = MonthlyWindow(date(2024, 1, 1), date(2024, 2, 1))
    now = datetime(2024, 3, 1, tzinfo=UTC)
    row = MonthlyObservation(MacroSeries.CPI, window.start, "2024-M01", Decimal("10"))
    for rows, started, received in [
        ((row,), now.replace(tzinfo=None), now),
        ((row,), now, now - timedelta(seconds=1)),
        ((row, row), now, now),
    ]:
        with pytest.raises(ValueError):
            ProviderRead(window, rows, started, received)
