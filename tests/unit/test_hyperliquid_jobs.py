"""Funding window planning and CLI failures before database/configuration access."""

from datetime import UTC, datetime, timedelta

import pytest

from market_intelligence import cli
from market_intelligence.hyperliquid.models import expected_hours, funding_windows


def test_month_chunks_preserve_leap_year_and_partial_receipt_window() -> None:
    start = datetime(2024, 1, 31, tzinfo=UTC)
    end = datetime(2024, 3, 1, 2, 0, 0, 123000, tzinfo=UTC)
    windows = funding_windows(start, end)
    assert [expected_hours(window) for window in windows] == [24, 696, 3]
    assert windows[-1].end == end


@pytest.mark.parametrize(
    "args",
    [
        ["ingest-funding", "--refresh", "--resume"],
        ["ingest-funding", "--refresh", "--start", "2024-01-01"],
    ],
)
def test_invalid_replay_flags_fail_before_configuration(
    args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden() -> None:
        pytest.fail("Configuration should not be accessed")

    monkeypatch.setattr(cli, "DatabaseSettings", forbidden)
    with pytest.raises(SystemExit) as caught:
        cli.main(args)
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "start,end",
    [
        (datetime(2023, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC)),
        (datetime(2024, 1, 1), datetime(2024, 2, 1, tzinfo=UTC)),
        (datetime(2024, 1, 1, 0, 1, tzinfo=UTC), datetime(2024, 2, 1, tzinfo=UTC)),
    ],
)
def test_invalid_job_windows(start: datetime, end: datetime) -> None:
    with pytest.raises(ValueError):
        funding_windows(start, end)


def test_december_boundaries() -> None:
    windows = funding_windows(datetime(2024, 12, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC))
    assert len(windows) == 1 and windows[0].end - windows[0].start == timedelta(days=31)
