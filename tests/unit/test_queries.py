import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from market_intelligence.config import ApiSettings
from market_intelligence.ingestion.models import TimeWindow, parse_instant
from market_intelligence.queries.models import (
    QueryValidationError,
    cursor_after,
    cursor_for,
    validate_window,
)

START = parse_instant("2024-01-01")
END = START + timedelta(hours=1)
WINDOW = TimeWindow(START, END)


@pytest.mark.parametrize("resolution", [300, 900, 3600, 86400])
def test_window_accepts_supported_epoch_grid(resolution: int) -> None:
    end = START + timedelta(seconds=resolution)
    assert validate_window(START, end, resolution, 3653) == TimeWindow(START, end)


@pytest.mark.parametrize(
    "start,end,resolution",
    [
        (datetime(2024, 1, 1), END, 300),
        (START, START, 300),
        (END, START, 300),
        (START + timedelta(seconds=1), END, 300),
        (START, END, 60),
        (START + timedelta(minutes=5), END, 900),
        (START, START + timedelta(hours=1), 86400),
    ],
)
def test_invalid_windows_are_rejected(start: datetime, end: datetime, resolution: int) -> None:
    with pytest.raises(QueryValidationError):
        validate_window(start, end, resolution, 3653)


def test_width_limit_does_not_limit_historical_age() -> None:
    start = datetime(2010, 1, 1, tzinfo=UTC)
    assert validate_window(start, start + timedelta(days=1), 300, 1).expected == 288
    with pytest.raises(QueryValidationError):
        validate_window(start, start + timedelta(days=1, minutes=5), 300, 1)


def test_cursor_round_trip_and_binding() -> None:
    cursor = cursor_for(1, WINDOW, 300, START)
    assert cursor_after(cursor, 1, WINDOW, 300) == START
    for market_id, window, resolution in [
        (2, WINDOW, 300),
        (1, TimeWindow(START, END + timedelta(hours=1)), 300),
        (1, WINDOW, 900),
    ]:
        with pytest.raises(QueryValidationError):
            cursor_after(cursor, market_id, window, resolution)


@pytest.mark.parametrize("token", ["!", "a" * 1025, "W10=", "bnVsbA==", "e30="])
def test_malformed_cursor_is_safely_rejected(token: str) -> None:
    with pytest.raises(QueryValidationError):
        cursor_after(token, 1, WINDOW, 300)


@pytest.mark.parametrize(
    "after",
    ["2023-12-31T23:55:00Z", "2024-01-01T01:00:00Z", "2024-01-01T00:00:01Z", "2024-01-01T00:00:00"],
)
def test_decoded_cursor_timestamp_is_validated(after: str) -> None:
    data = [1, 1, START.isoformat(), END.isoformat(), 300, after]
    token = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    with pytest.raises(QueryValidationError):
        cursor_after(token, 1, WINDOW, 300)


def test_api_limits_are_environment_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_MAX_WINDOW_DAYS", "20")
    monkeypatch.setenv("API_STALE_AFTER_SECONDS", "600")
    settings = ApiSettings(_env_file=None)  # type: ignore[call-arg]
    assert settings.max_window_days == 20 and settings.stale_after_seconds == 600
