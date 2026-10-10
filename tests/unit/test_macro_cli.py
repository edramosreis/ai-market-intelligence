from datetime import UTC, date, datetime
from unittest.mock import Mock

import pytest

from market_intelligence import cli
from market_intelligence.macro import cli as macro_cli
from market_intelligence.macro.jobs import MacroIngestionError
from market_intelligence.macro.models import MacroProvider, MonthlyWindow
from market_intelligence.macro.storage_models import MacroFailureCode

NOW = datetime(2026, 10, 8, tzinfo=UTC)


@pytest.mark.parametrize(
    "command,arguments,start,resume",
    [
        ("ingest-bls", [], date(1947, 1, 1), False),
        ("ingest-bls", ["--resume"], date(1947, 1, 1), True),
        ("ingest-bls", ["--refresh"], date(2021, 1, 1), False),
        ("ingest-fed", [], date(1954, 7, 1), False),
        ("ingest-fed", ["--resume"], date(1954, 7, 1), True),
        ("ingest-fed", ["--refresh"], date(1954, 7, 1), False),
    ],
)
def test_cli_native_history_and_provider_specific_refresh(
    command: str,
    arguments: list[str],
    start: date,
    resume: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = Mock()
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)
    monkeypatch.setattr(cli, "DatabaseSettings", Mock())
    monkeypatch.setattr(macro_cli, "run", runner)
    assert cli.main([command, *arguments]) == 0
    plan = runner.call_args.args[0]
    assert plan.window == MonthlyWindow(start, date(2026, 10, 1)) and plan.resume is resume
    assert plan.max_requests == (12 if command == "ingest-bls" else 3)


@pytest.mark.parametrize("command", ["ingest-bls", "ingest-fed"])
@pytest.mark.parametrize(
    "arguments",
    [
        ["--refresh", "--resume"],
        ["--refresh", "--end", "2024-02-01"],
        ["--refresh", "--start", "2024-01-01"],
    ],
)
def test_conflicting_modes_are_rejected_before_configuration(
    command: str,
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Mock(side_effect=AssertionError("Configuration must not be read"))
    monkeypatch.setattr(cli, "DatabaseSettings", settings)
    with pytest.raises(SystemExit) as caught:
        cli.main([command, *arguments])
    assert caught.value.code == 2
    settings.assert_not_called()


@pytest.mark.parametrize(
    "command,arguments",
    [
        ("ingest-bls", ["--start", "1946-12-01"]),
        ("ingest-fed", ["--start", "1954-06-01"]),
        ("ingest-bls", ["--start", "20240101"]),
        ("ingest-fed", ["--start", "2024-01-02"]),
        ("ingest-bls", ["--start", "2024-01-01T00:00:00Z"]),
        ("ingest-fed", ["--end", "2026-11-01"]),
        ("ingest-bls", ["--request-interval", "2"]),
        ("ingest-fed", ["--request-interval", "nan"]),
        ("ingest-bls", ["--window-seconds", "0"]),
        ("ingest-fed", ["--max-seconds", "inf"]),
        ("ingest-bls", ["--max-requests", "0"]),
        ("ingest-fed", ["--max-requests", "26"]),
    ],
)
def test_bad_bounds_and_budgets_fail_before_configuration_or_network(
    command: str,
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, runner = Mock(), Mock()
    monkeypatch.setattr(cli, "DatabaseSettings", settings)
    monkeypatch.setattr(cli, "utc_now", lambda: NOW)
    monkeypatch.setattr(macro_cli, "run", runner)
    with pytest.raises(SystemExit) as caught:
        cli.main([command, *arguments])
    assert caught.value.code == 1
    settings.assert_not_called()
    runner.assert_not_called()


@pytest.mark.parametrize("request_limit,exhausted", [(1, True), (3, False)])
def test_request_budget_and_retry_failure_are_distinguished_in_sanitized_output(
    request_limit: int,
    exhausted: bool,
) -> None:
    error = MacroIngestionError(
        MacroFailureCode.RETRY_EXHAUSTED,
        MacroProvider.BLS,
        MonthlyWindow(date(2024, 1, 1), date(2025, 1, 1)),
    )
    error.requests_used, error.request_limit = 1, request_limit
    result = macro_cli.failure_report(error)
    assert result["request_budget_exhausted"] is exhausted and result["code"] == "retry_exhausted"
    assert "--start 2024-01-01 --end 2025-01-01 without --resume" in str(result["recovery"])
