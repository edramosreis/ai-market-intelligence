from datetime import UTC, date, datetime
from unittest.mock import Mock

import pytest

from market_intelligence.db.macro_store import MacroStore
from market_intelligence.macro.jobs import (
    HISTORY_START,
    expected_periods,
    job_windows,
    refresh_start,
    resume_eligible,
)
from market_intelligence.macro.models import MacroProvider, MacroSeries, MonthlyWindow
from market_intelligence.macro.service import ingest_macro

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def test_native_history_uses_eight_bls_requests_and_one_fed_release() -> None:
    end = date(2026, 10, 1)
    bls = job_windows(MacroProvider.BLS, MonthlyWindow(HISTORY_START[MacroProvider.BLS], end), NOW)
    assert len(bls) == 8 and bls[0] == MonthlyWindow(date(1947, 1, 1), date(1957, 1, 1))
    assert bls[-1] == MonthlyWindow(date(2017, 1, 1), end)
    assert all(left.end == right.start for left, right in zip(bls, bls[1:], strict=False))
    fed = MonthlyWindow(HISTORY_START[MacroProvider.FED], end)
    assert job_windows(MacroProvider.FED, fed, NOW) == (fed,)


def test_partial_bls_year_bounds_do_not_request_eleven_inclusive_years() -> None:
    window = MonthlyWindow(date(2000, 7, 1), date(2010, 7, 1))
    assert job_windows(MacroProvider.BLS, window, NOW) == (
        MonthlyWindow(date(2000, 7, 1), date(2010, 1, 1)),
        MonthlyWindow(date(2010, 1, 1), date(2010, 7, 1)),
    )


@pytest.mark.parametrize(
    "provider,window",
    [
        (MacroProvider.BLS, MonthlyWindow(date(1946, 12, 1), date(1947, 2, 1))),
        (MacroProvider.FED, MonthlyWindow(date(1954, 6, 1), date(1954, 8, 1))),
        (MacroProvider.BLS, MonthlyWindow(date(2026, 9, 1), date(2026, 11, 1))),
        (MacroProvider.FED, MonthlyWindow(date(2026, 9, 1), date(2026, 11, 1))),
    ],
)
def test_invalid_native_and_unfinished_bounds_fail_before_fetch_or_database(
    provider: MacroProvider,
    window: MonthlyWindow,
) -> None:
    source, store = Mock(), Mock(spec=MacroStore)
    with pytest.raises(ValueError):
        ingest_macro(source, store, provider, window, now=lambda: NOW)
    source.fetch.assert_not_called()
    store.start_run.assert_not_called()


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
def test_invalid_job_budgets_fail_before_fetch_or_database(seconds: float) -> None:
    source, store = Mock(), Mock(spec=MacroStore)
    window = MonthlyWindow(date(2024, 1, 1), date(2025, 1, 1))
    for window_seconds, max_seconds in ((seconds, 900), (180, seconds)):
        with pytest.raises(ValueError):
            ingest_macro(
                source,
                store,
                MacroProvider.BLS,
                window,
                now=lambda: NOW,
                window_seconds=window_seconds,
                max_seconds=max_seconds,
            )
    source.fetch.assert_not_called()
    store.start_run.assert_not_called()


def test_native_month_expectations_exclude_pre_series_months() -> None:
    periods = expected_periods(
        MacroProvider.BLS, MonthlyWindow(date(1947, 12, 1), date(1948, 2, 1))
    )
    assert periods == {
        (MacroSeries.CPI, date(1947, 12, 1)),
        (MacroSeries.CPI, date(1948, 1, 1)),
        (MacroSeries.UNEMPLOYMENT, date(1948, 1, 1)),
    }


def test_refresh_and_resume_revisit_the_revision_region_and_latest_month() -> None:
    assert refresh_start(MacroProvider.BLS, NOW.date()) == date(2021, 1, 1)
    assert refresh_start(MacroProvider.FED, NOW.date()) == date(1954, 7, 1)
    assert resume_eligible(
        MacroProvider.BLS, MonthlyWindow(date(2020, 1, 1), date(2021, 1, 1)), NOW
    )
    assert not resume_eligible(
        MacroProvider.BLS, MonthlyWindow(date(2021, 1, 1), date(2022, 1, 1)), NOW
    )
    assert not resume_eligible(
        MacroProvider.FED, MonthlyWindow(date(2026, 9, 1), date(2026, 10, 1)), NOW
    )
    assert resume_eligible(
        MacroProvider.FED, MonthlyWindow(date(2024, 1, 1), date(2025, 1, 1)), NOW
    )
