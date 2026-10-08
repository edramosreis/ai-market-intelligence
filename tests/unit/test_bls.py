import copy
import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from market_intelligence.macro.bls import BLS_URL, BlsClient, parse_bls, request_years
from market_intelligence.macro.models import MacroError, MacroSeries, MonthlyWindow

WINDOW = MonthlyWindow(date(2024, 1, 1), date(2025, 1, 1))


def row(month: str = "M01", value: str = "10.250", **extra: Any) -> dict[str, Any]:
    return {
        "year": "2024",
        "period": month,
        "periodName": {"M01": "January", "M02": "February", "M13": "Annual"}[month],
        "value": value,
        "footnotes": [{}],
        **extra,
    }


def envelope() -> dict[str, Any]:
    return {
        "status": "REQUEST_SUCCEEDED",
        "responseTime": 1,
        "message": [],
        "Results": {
            "series": [
                {"seriesID": "CUSR0000SA0", "data": [row("M02"), row()]},
                {"seriesID": "LNS14000000", "data": [row(value="0")]},
            ],
        },
    }


def test_native_values_missing_footnotes_annual_average_and_hint() -> None:
    body = envelope()
    body["Results"]["series"][0]["data"] = [
        row("M02", "-", latest="true", footnotes=[{"code": "X", "text": "Synthetic lapse"}]),
        row(),
        row("M13", "11.125"),
    ]
    observations, annual, messages, hints = parse_bls(json.dumps(body).encode(), WINDOW)
    assert [(obs.series, obs.month) for obs in observations] == [
        (MacroSeries.CPI, date(2024, 1, 1)),
        (MacroSeries.CPI, date(2024, 2, 1)),
        (MacroSeries.UNEMPLOYMENT, date(2024, 1, 1)),
    ]
    assert observations[0].value == Decimal("10.250") and not observations[0].footnotes
    assert observations[1].value is None and observations[1].missing_reason == "source_dash"
    assert observations[1].footnotes[0].text == "Synthetic lapse"
    assert observations[2].value == 0
    assert annual == 1 and not messages and hints == ((MacroSeries.CPI, date(2024, 2, 1)),)
    del body["Results"]["series"][0]["data"][0]["latest"]
    assert parse_bls(json.dumps(body).encode(), WINDOW)[0] == observations


def test_year_request_filters_logical_month_window_and_preserves_empty_series() -> None:
    body = envelope()
    body["Results"]["series"][1]["data"] = []
    body["message"] = ["No Data Available for Series LNS14000000 Year: 2024"]
    window = MonthlyWindow(date(2024, 2, 1), date(2024, 3, 1))
    rows, _, messages, _ = parse_bls(json.dumps(body).encode(), window)
    assert len(rows) == 1 and rows[0].month == window.start
    assert messages == tuple(body["message"])
    assert request_years(MonthlyWindow(date(1947, 1, 1), date(1957, 1, 1))) == (1947, 1956)
    for bounds in [(date(1946, 1, 1), date(1947, 1, 1)), (date(2020, 2, 1), date(2030, 2, 1))]:
        with pytest.raises(ValueError):
            request_years(MonthlyWindow(*bounds))


@pytest.mark.parametrize(
    "change",
    [
        {"year": "2023"},
        {"year": 2024},
        {"period": "M00"},
        {"period": "M14"},
        {"periodName": "February"},
        {"value": 10},
        {"value": "NaN"},
        {"value": "1e2"},
        {"value": "0"},
        {"value": "10.0000000000000000001"},
        {"latest": True},
        {"latest": "false"},
        {"footnotes": [{"code": "X"}]},
        {"footnotes": [{"code": "", "text": "Private upstream content"}]},
        {"releaseTime": "invented"},
    ],
)
def test_rejects_invalid_monthly_fields_even_outside_logical_window(change: dict[str, Any]) -> None:
    body = envelope()
    body["Results"]["series"][0]["data"][1].update(change)
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_bls(json.dumps(body).encode(), MonthlyWindow(date(2024, 2, 1), date(2024, 3, 1)))


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_month",
        "duplicate_series",
        "wrong_series",
        "missing_series",
        "hint_twice",
        "bad_results",
        "unknown_status",
    ],
)
def test_rejects_ambiguous_envelope(case: str) -> None:
    body = envelope()
    series = body["Results"]["series"]
    if case == "duplicate_month":
        series[0]["data"].append(copy.deepcopy(series[0]["data"][0]))
    elif case == "duplicate_series":
        series[1] = copy.deepcopy(series[0])
    elif case == "wrong_series":
        series[1]["seriesID"] = "RIFSPFF_N.M"
    elif case == "missing_series":
        series.pop()
    elif case == "hint_twice":
        for item in series[0]["data"]:
            item["latest"] = "true"
    elif case == "bad_results":
        body["Results"] = [body["Results"]]
    else:
        body["status"] = "unknown"
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_bls(json.dumps(body).encode(), WINDOW)


@pytest.mark.parametrize("payload", [b'{"status":1,"status":2}', b"{", b"\xff", b"[]"])
def test_malformed_or_duplicate_json_is_sanitized(payload: bytes) -> None:
    with pytest.raises(MacroError, match="^invalid_payload$"):
        parse_bls(payload, WINDOW)


def test_source_rejection_is_sanitized_without_transport_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = envelope()
        body["status"] = "REQUEST_NOT_PROCESSED"
        body["message"] = ["Private error details"]
        return httpx.Response(200, json=body)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        pytest.raises(MacroError, match="^source_rejected$"),
    ):
        BlsClient(http, monotonic=lambda: 0).fetch(WINDOW, deadline=100)
    assert calls == 1


def test_fixed_keyless_request_and_receipt() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and str(request.url) == BLS_URL
        assert json.loads(request.content) == {
            "seriesid": ["CUSR0000SA0", "LNS14000000"],
            "startyear": "2024",
            "endyear": "2024",
        }
        assert "authorization" not in request.headers
        return httpx.Response(200, json=envelope())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = BlsClient(http, monotonic=lambda: 0).fetch(WINDOW, deadline=100)
    assert len(result.observations) == 3 and result.received_at >= result.fetch_started_at
