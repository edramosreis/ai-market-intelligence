"""Synthetic funding pagination, native units, OI mapping, and bounded transport."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest

from market_intelligence.hyperliquid.client import (
    MAX_RESPONSE_BYTES,
    HyperliquidClient,
    parse_funding,
    parse_open_interest,
)
from market_intelligence.hyperliquid.models import (
    FundingEvent,
    FundingWindow,
    HyperliquidError,
    HyperliquidErrorCode,
    OpenInterestSnapshot,
    milliseconds,
)

START = datetime(2024, 1, 1, tzinfo=UTC)
WINDOW = FundingWindow(START, START + timedelta(days=31))
IDENTITY = UUID("00000000-0000-0000-0000-000000000001")


def event(index: int = 0, **changes: Any) -> dict[str, Any]:
    return {
        "coin": "BTC",
        "time": milliseconds(START + timedelta(hours=index, milliseconds=76)),
        "fundingRate": "-0.000012500000000001",
        "premium": "0.0001",
        **changes,
    }


def contexts() -> list[Any]:
    return [
        {"universe": [{"name": "ETH"}, {"name": "BTC"}], "collateralToken": 0},
        [
            {"openInterest": "999"},
            {"openInterest": "0", "markPx": "42000.125", "oraclePx": "42001.25", "funding": "0.99"},
        ],
    ]


def parse_context(value: Any) -> OpenInterestSnapshot:
    return parse_open_interest(
        json.dumps(value).encode(),
        snapshot_id=IDENTITY,
        fetch_started_at=START,
        received_at=START + timedelta(seconds=2),
    )


class Clock:
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


def test_funding_preserves_offsets_signed_fractions_and_zero() -> None:
    values = parse_funding(json.dumps([event(), event(1, fundingRate="0")]).encode(), WINDOW)
    assert values[0].event_at == START + timedelta(milliseconds=76)
    assert values[0].settlement_hour == START
    assert values[0].funding_rate == Decimal("-0.000012500000000001")
    assert values[0].premium == Decimal("0.0001")
    assert values[1].funding_rate == 0
    assert parse_funding(b"[]", WINDOW) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"coin": "ETH"},
        {"time": True},
        {"time": 1704067200076.0},
        {"time": -1},
        {"time": 10**50},
        {"time": milliseconds(WINDOW.end)},
        {"extra": "new metric"},
        {"fundingRate": None},
        {"fundingRate": 0.01},
        {"fundingRate": "NaN"},
        {"fundingRate": "1e-5"},
        {"premium": "Infinity"},
        {"fundingRate": "0.0000000000000000001"},
        {"fundingRate": "100000000000000000000"},
    ],
)
def test_invalid_funding_fails_safely(changes: dict[str, Any]) -> None:
    with pytest.raises(HyperliquidError, match="^invalid_payload$"):
        parse_funding(json.dumps([event(**changes)]).encode(), WINDOW)


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[null]",
        b"<html>secret</html>",
        b'[{"coin":"BTC","coin":"ETH"}]',
        b"x" * (MAX_RESPONSE_BYTES + 1),
    ],
    ids=["object", "null", "html", "duplicate-key", "oversized"],
)
def test_invalid_json_and_duplicate_properties(payload: bytes) -> None:
    with pytest.raises(HyperliquidError, match="^invalid_payload$"):
        parse_funding(payload, WINDOW)


@pytest.mark.parametrize(
    "rows",
    [
        [event(), event()],
        [event(1), event()],
        [event(), event(time=milliseconds(START + timedelta(milliseconds=77)))],
        [event(index) for index in range(501)],
    ],
)
def test_duplicates_order_and_page_limit(rows: list[dict[str, Any]]) -> None:
    with pytest.raises(HyperliquidError):
        parse_funding(json.dumps(rows).encode(), WINDOW)


def test_oi_uses_named_context_native_units_and_receipt_interval() -> None:
    value = parse_context(contexts())
    assert value.snapshot_id == IDENTITY
    assert value.open_interest_btc == 0
    assert value.mark_price_usdt == Decimal("42000.125")
    assert value.oracle_price_usdt == Decimal("42001.25")
    assert value.fetch_started_at == START
    assert value.received_at == START + timedelta(seconds=2)
    assert not hasattr(value, "event_at")
    assert not hasattr(value, "funding_rate")  # dynamic context funding is not settlement history


@pytest.mark.parametrize(
    "change",
    [
        "short",
        "duplicate",
        "missing",
        "delisted",
        "collateral",
        "negative",
        "no_price",
        "float",
        "zero_price",
    ],
)
def test_ambiguous_or_invalid_context_is_rejected(change: str) -> None:
    value = contexts()
    if change == "short":
        value[1].pop()
    elif change == "duplicate":
        value[0]["universe"][0]["name"] = "BTC"
    elif change == "missing":
        value[0]["universe"][1]["name"] = "XBT"
    elif change == "delisted":
        value[0]["universe"][1]["isDelisted"] = True
    elif change == "collateral":
        value[0]["collateralToken"] = 1
    elif change == "negative":
        value[1][1]["openInterest"] = "-1"
    elif change == "no_price":
        value[1][1].pop("markPx")
    elif change == "float":
        value[1][1]["openInterest"] = 1.1
    elif change == "zero_price":
        value[1][1]["oraclePx"] = "0"
    with pytest.raises(HyperliquidError, match="^invalid_payload$"):
        parse_context(value)


def test_source_time_and_local_time_domains() -> None:
    with pytest.raises(ValueError):
        FundingWindow(START.replace(tzinfo=None), WINDOW.end)
    with pytest.raises(ValueError):
        FundingWindow(START, START + timedelta(days=32))
    with pytest.raises(ValueError):
        FundingEvent(START + timedelta(microseconds=1), Decimal(0), Decimal(0))
    with pytest.raises(ValueError):
        OpenInterestSnapshot(
            IDENTITY, START, START - timedelta(seconds=1), Decimal(1), Decimal(1), Decimal(1)
        )


def test_inclusive_pagination_removes_only_exact_overlap_and_paces() -> None:
    requests: list[dict[str, Any]] = []
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        indexes = range(500) if len(requests) == 1 else range(499, 744)
        return httpx.Response(200, json=[event(index) for index in indexes])

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HyperliquidClient(
            http, monotonic=clock.monotonic, sleep=clock.sleep, now=clock.now
        )
        values = client.fetch_funding(WINDOW, deadline=100)
    assert len(values) == 744 and len(requests) == 2
    assert requests[1]["startTime"] == event(499)["time"]
    assert requests[0]["endTime"] == milliseconds(WINDOW.end) - 1
    assert clock.sleeps == [3.0]


def test_pagination_rejects_changed_boundary() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        rows = [event(index) for index in range(500)] if calls == 1 else [event(499, premium="0.9")]
        return httpx.Response(200, json=rows)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        pytest.raises(HyperliquidError, match="^invalid_payload$"),
    ):
        HyperliquidClient(http, request_interval=0).fetch_funding(WINDOW, deadline=10**10)


def test_throttle_retry_and_oi_request_identity() -> None:
    calls = 0
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert str(request.url) == "https://api.hyperliquid.xyz/info"
        assert json.loads(request.content) == {"type": "metaAndAssetCtxs", "dex": ""}
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "5"})
        clock.elapsed += 2
        return httpx.Response(200, json=contexts())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        value = HyperliquidClient(
            http,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            now=clock.now,
            snapshot_id=lambda: IDENTITY,
        ).fetch_open_interest(deadline=100)
    assert clock.sleeps == [5.0]
    assert value.fetch_started_at == START + timedelta(seconds=5)
    assert value.received_at == START + timedelta(seconds=7)


@pytest.mark.parametrize(
    "status,code,calls",
    [
        (302, "http_error", 1),
        (400, "http_error", 1),
        (429, "retry_exhausted", 3),
        (503, "retry_exhausted", 3),
    ],
)
def test_http_errors_sanitized_and_retry_bounded(status: int, code: str, calls: int) -> None:
    clock = Clock()
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(status, text="private upstream body")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        pytest.raises(HyperliquidError, match=f"^{code}$"),
    ):
        HyperliquidClient(
            http, monotonic=clock.monotonic, sleep=clock.sleep, jitter=lambda: 0
        ).fetch_funding(WINDOW, deadline=100)
    assert count == calls


class Stream(httpx.SyncByteStream):
    def __init__(self, clock: Clock, oversized: bool) -> None:
        self.clock, self.oversized = clock, oversized

    def __iter__(self) -> Iterator[bytes]:
        yield b"["
        if self.oversized:
            yield b"x" * MAX_RESPONSE_BYTES
        else:
            self.clock.elapsed = 100
            yield b"]"


@pytest.mark.parametrize(
    "oversized,code", [(True, "invalid_payload"), (False, "deadline_exceeded")]
)
def test_stream_bounds_and_deadline(oversized: bool, code: str) -> None:
    clock = Clock()
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=Stream(clock, oversized))
            )
        ) as http,
        pytest.raises(HyperliquidError, match=f"^{code}$"),
    ):
        HyperliquidClient(http, monotonic=clock.monotonic, sleep=clock.sleep).fetch_funding(
            WINDOW, deadline=10
        )


def test_deadline_prevents_request_and_retry_sleep() -> None:
    clock = Clock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "20"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = HyperliquidClient(http, monotonic=clock.monotonic, sleep=clock.sleep)
        with pytest.raises(HyperliquidError) as caught:
            client.fetch_funding(WINDOW, deadline=0)
        assert caught.value.code == HyperliquidErrorCode.DEADLINE_EXCEEDED and calls == 0
        with pytest.raises(HyperliquidError, match="^deadline_exceeded$"):
            client.fetch_open_interest(deadline=10)
    assert calls == 1 and not clock.sleeps
