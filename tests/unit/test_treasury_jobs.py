from datetime import UTC, date, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import Engine

from market_intelligence import cli
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


def test_refresh_resume_is_rejected_before_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Mock(side_effect=AssertionError("Configuration must not be read"))
    monkeypatch.setattr(cli, "DatabaseSettings", settings)
    with pytest.raises(SystemExit) as caught:
        cli.main(["ingest-treasury", "--refresh", "--resume"])
    assert caught.value.code == 2
    assert "--refresh cannot be combined with --resume" in capsys.readouterr().err
    settings.assert_not_called()


@pytest.mark.parametrize(
    ("arguments", "today", "start", "end", "resume"),
    [
        ([], datetime(2026, 10, 6, tzinfo=UTC), date(1990, 1, 1), date(2026, 11, 1), False),
        (
            ["--resume"],
            datetime(2026, 10, 6, tzinfo=UTC),
            date(1990, 1, 1),
            date(2026, 11, 1),
            True,
        ),
        (
            ["--start", "2024-01-01", "--end", "2024-02-01"],
            datetime(2026, 10, 6, tzinfo=UTC),
            date(2024, 1, 1),
            date(2024, 2, 1),
            False,
        ),
        (
            ["--refresh"],
            datetime(2026, 10, 6, tzinfo=UTC),
            date(2026, 9, 1),
            date(2026, 11, 1),
            False,
        ),
        (
            ["--refresh"],
            datetime(2026, 1, 1, tzinfo=UTC),
            date(2025, 12, 1),
            date(2026, 2, 1),
            False,
        ),
    ],
)
def test_cli_uses_full_treasury_history_and_explicit_replay_bounds(
    arguments: list[str],
    today: datetime,
    start: date,
    end: date,
    resume: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = Mock(spec=Engine)
    ingestion = Mock(return_value=[])
    monkeypatch.setattr(cli, "utc_now", lambda: today)
    monkeypatch.setattr(cli, "DatabaseSettings", Mock())
    monkeypatch.setattr(cli, "create_db_engine", Mock(return_value=engine))
    monkeypatch.setattr(cli, "ingest_treasury", ingestion)
    assert cli.main(["ingest-treasury", *arguments]) == 0
    assert ingestion.call_args.args[2:] == (start, end)
    assert ingestion.call_args.kwargs["resume"] is resume
    engine.dispose.assert_called_once()
