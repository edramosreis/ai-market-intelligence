from datetime import date, datetime

import pytest

from market_intelligence.treasury.models import TreasuryMonth, treasury_months


def test_half_open_month_windows_across_year_and_leap_day() -> None:
    months = treasury_months(date(2023, 12, 1), date(2024, 3, 1))
    assert [month.provider_month for month in months] == ["202312", "202401", "202402"]
    assert (months[-1].end - months[-1].start).days == 29
    assert treasury_months(date(1990, 1, 1), date(1990, 2, 1)) == [TreasuryMonth(1990, 1)]


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2024, 1, 2), date(2024, 2, 1)),
        (date(2024, 1, 1), date(2024, 2, 2)),
        (date(2024, 2, 1), date(2024, 2, 1)),
        (date(2024, 3, 1), date(2024, 2, 1)),
        (date(1989, 12, 1), date(1990, 1, 1)),
        (datetime(2024, 1, 1), date(2024, 2, 1)),
    ],
)
def test_invalid_month_bounds(start: date, end: date) -> None:
    with pytest.raises(ValueError, match="month-aligned"):
        treasury_months(start, end)
