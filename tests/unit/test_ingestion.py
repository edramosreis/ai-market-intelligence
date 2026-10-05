"""Wire-format and retry tests use synthetic payloads and an injected clock."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from market_intelligence.ingestion.coinbase import BASE_URL, CoinbaseClient, parse_candles
from market_intelligence.ingestion.models import (
    ErrorCode,
    IngestionError,
    TimeWindow,
    closed_cutoff,
    monthly_windows,
    parse_instant,
)

START = datetime(2024, 1, 1, tzinfo=UTC)
WINDOW = TimeWindow(START, START + timedelta(minutes=5))
ROW: list[Any] = [int(START.timestamp()), 100, 120, 110, 115, Decimal("1.123456789123456789")]


class FakeClock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds

    def now(self) -> datetime:
        return START + timedelta(seconds=self.elapsed)


def test_closed_cutoff_keeps_one_minute_settling_allowance() -> None:
    assert closed_cutoff(START + timedelta(minutes=5, seconds=59)) == START
    assert closed_cutoff(START + timedelta(minutes=6)) == WINDOW.end


def test_cli_date_shorthand_and_explicit_offset() -> None:
    assert parse_instant("2024-01-01") == START
    assert parse_instant("2024-01-01T03:00:00+03:00") == START
    with pytest.raises(ValueError):
        parse_instant("2024-01-01T00:00:00")


@pytest.mark.parametrize(
    "start,end",
    [
        (START.replace(tzinfo=None), WINDOW.end),
        (START + timedelta(seconds=1), WINDOW.end),
        (START, START),
        (WINDOW.end, START),
    ],
)
def test_window_rejects_naive_unaligned_or_reversed_ranges(start: datetime, end: datetime) -> None:
    with pytest.raises(ValueError):
        TimeWindow(start, end)


def test_monthly_chunks_clip_boundaries_and_handle_leap_years() -> None:
    window = TimeWindow(
        parse_instant("2024-01-31T23:55:00Z"), parse_instant("2024-03-01T00:05:00Z")
    )
    chunks = monthly_windows(window)
    assert [chunk.expected for chunk in chunks] == [1, 29 * 288, 1]
    assert chunks[0].start == window.start and chunks[-1].end == window.end
    assert all(left.end == right.start for left, right in zip(chunks, chunks[1:], strict=False))
    december = TimeWindow(parse_instant("2023-12-31T23:55:00Z"), WINDOW.end)
    assert len(monthly_windows(december)) == 2


def test_wire_order_exact_decimals_sorting_deduplication_and_filtering() -> None:
    second = ROW.copy()
    second[0] += 300
    before = [ROW[0] - 300, "invalid-outside-range", 0, 0, 0, 0]
    current = ROW.copy()
    current[0] += 600
    window = TimeWindow(START, START + timedelta(minutes=15))
    result = parse_candles(
        [current, second, ROW, ROW, before], window, START + timedelta(minutes=10)
    )
    assert [c.opened_at for c in result] == [START, START + timedelta(minutes=5)]
    assert (result[0].open, result[0].high, result[0].low, result[0].close) == tuple(
        Decimal(x) for x in (110, 120, 100, 115)
    )
    assert result[0].base_volume == ROW[5]


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", Decimal(0)),
        ("base_volume", Decimal(-1)),
        ("open", Decimal("NaN")),
        ("high", Decimal("Infinity")),
        ("open", Decimal("1e20")),
        ("base_volume", Decimal("1e-19")),
        ("base_volume", 0.1),
        ("low", Decimal(116)),
        ("opened_at", START + timedelta(seconds=1)),
        ("opened_at", START.replace(tzinfo=None)),
    ],
)
def test_candle_rejects_invalid_values_before_postgresql_can_coerce_them(
    field: str, value: Any
) -> None:
    candle = parse_candles([ROW], WINDOW, WINDOW.end)[0]
    with pytest.raises(IngestionError) as error:
        replace(candle, **{field: value})
    assert error.value.code == ErrorCode.INVALID_CANDLE


@pytest.mark.parametrize("payload", [{}, [ROW[:-1]], [[True, *ROW[1:]]], [ROW, [*ROW[:5], 2]]])
def test_malformed_or_conflicting_rows_fail(payload: object) -> None:
    with pytest.raises(IngestionError):
        parse_candles(payload, WINDOW, WINDOW.end)


def test_empty_provider_response_preserves_gap() -> None:
    assert parse_candles([], WINDOW, WINDOW.end) == []


def test_json_decoding_does_not_pass_through_a_binary_float() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"[[1704067200,100,120,110,115,0.123456789123456789]]")

    with httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as http:
        source = CoinbaseClient(http, request_interval=0)
        result = source.fetch_chunk(WINDOW, WINDOW.end, source.monotonic() + 10)
    assert result[0].base_volume == Decimal("0.123456789123456789")


def test_page_requests_are_half_open_and_never_exceed_250_buckets() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    with httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as http:
        source = CoinbaseClient(http, request_interval=0)
        window = TimeWindow(START, START + timedelta(minutes=5 * 501))
        assert source.fetch_chunk(window, window.end, source.monotonic() + 10) == []
    assert len(requests) == 3
    ends = [parse_instant(request.url.params["end"]) for request in requests]
    starts = [parse_instant(request.url.params["start"]) for request in requests]
    assert starts[0] == START and ends[-1] == window.end
    assert ends[:-1] == starts[1:]
    assert all(
        (end - start) <= timedelta(minutes=1250) for start, end in zip(starts, ends, strict=True)
    )


@pytest.mark.parametrize("kind", ["timeout", "connection", "429", "500", "502", "503", "504"])
def test_transient_failures_retry_with_injected_time(kind: str) -> None:
    clock = FakeClock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if kind == "timeout":
                raise httpx.ReadTimeout("synthetic timeout")
            if kind == "connection":
                raise httpx.ConnectError("synthetic connection failure")
            return httpx.Response(int(kind), headers={"Retry-After": "3"})
        return httpx.Response(200, json=[])

    with httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as http:
        source = CoinbaseClient(
            http, monotonic=clock.monotonic, sleep=clock.sleep, jitter=lambda: 0
        )
        source.get_json("/test", {}, 20)
    assert calls == 2
    expected_waits = [1] if kind in {"timeout", "connection"} else [3]
    assert clock.sleeps == expected_waits


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400),
        httpx.Response(404),
        httpx.Response(200, text="invalid JSON"),
        httpx.Response(200, text="[NaN]"),
    ],
)
def test_permanent_failures_do_not_retry(response: httpx.Response) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    with httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler)) as http:
        source = CoinbaseClient(http, request_interval=0)
        with pytest.raises(IngestionError):
            source.get_json("/test", {}, source.monotonic() + 20)
    assert calls == 1


def test_retry_exhaustion_and_deadline_limits() -> None:
    clock = FakeClock()
    with httpx.Client(
        base_url=BASE_URL, transport=httpx.MockTransport(lambda request: httpx.Response(429))
    ) as http:
        source = CoinbaseClient(
            http, monotonic=clock.monotonic, sleep=clock.sleep, jitter=lambda: 0
        )
        with pytest.raises(IngestionError) as error:
            source.get_json("/test", {}, 100)
        assert error.value.code == ErrorCode.RETRY_EXHAUSTED
        assert clock.sleeps == [1, 2, 4, 8]
        with pytest.raises(IngestionError) as error:
            source.get_json("/test", {}, clock.elapsed)
        assert error.value.code == ErrorCode.DEADLINE_EXCEEDED


def test_http_date_retry_after_and_unaffordable_wait() -> None:
    clock = FakeClock()
    with httpx.Client(
        base_url=BASE_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"Retry-After": "100"})
        ),
    ) as http:
        source = CoinbaseClient(http, monotonic=clock.monotonic, sleep=clock.sleep, now=clock.now)
        assert (
            source.retry_after(
                httpx.Response(429, headers={"Retry-After": "Mon, 01 Jan 2024 00:00:03 GMT"})
            )
            == 3
        )
        with pytest.raises(IngestionError) as error:
            source.get_json("/test", {}, 10)
        assert error.value.code == ErrorCode.DEADLINE_EXCEEDED
        assert not clock.sleeps


def test_response_arriving_after_deadline_is_not_accepted() -> None:
    clock = FakeClock()

    def slow(request: httpx.Request) -> httpx.Response:
        clock.elapsed = 11
        return httpx.Response(200, json=[])

    with httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(slow)) as http:
        source = CoinbaseClient(http, monotonic=clock.monotonic)
        with pytest.raises(IngestionError) as error:
            source.get_json("/test", {}, 10)
    assert error.value.code == ErrorCode.DEADLINE_EXCEEDED


def test_product_metadata_must_match_reviewed_spot_market() -> None:
    with httpx.Client(
        base_url=BASE_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "id": "BTC-USD",
                    "base_currency": "BTC",
                    "quote_currency": "USDT",
                    "margin_enabled": False,
                },
            )
        ),
    ) as http:
        source = CoinbaseClient(http)
        with pytest.raises(IngestionError) as error:
            source.verify_product(source.monotonic() + 10)
        assert error.value.code == ErrorCode.PRODUCT_MISMATCH
