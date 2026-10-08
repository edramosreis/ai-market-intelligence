from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from market_intelligence.macro._http import MacroTransport
from market_intelligence.macro.models import MacroError

START = datetime(2024, 1, 1, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return START + timedelta(seconds=self.elapsed)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds


@pytest.mark.parametrize("retry", ["5", format_datetime(START + timedelta(seconds=5), usegmt=True)])
def test_retry_after_and_successful_attempt_receipt(retry: str) -> None:
    clock, calls = Clock(), 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": retry})
        clock.elapsed += 2
        return httpx.Response(200, content=b"ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        transport = MacroTransport(
            http, monotonic=clock.monotonic, sleep=clock.sleep, now=clock.now
        )
        result = transport._request("GET", "https://example.test", max_bytes=20, deadline=100)
        assert result == (b"ok", START + timedelta(seconds=5), START + timedelta(seconds=7))
        transport._request("GET", "https://example.test", max_bytes=20, deadline=100)
    assert clock.sleeps == [5.0, 1.0]


@pytest.mark.parametrize(
    "status,code,calls",
    [(302, "http_error", 1), (400, "http_error", 1), (503, "retry_exhausted", 3)],
)
def test_sanitized_errors_redirects_and_retry_bound(status: int, code: str, calls: int) -> None:
    clock, count = Clock(), 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(
            status, headers={"Location": "https://other.test"}, text="Private details"
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http,
        pytest.raises(MacroError, match=f"^{code}$"),
    ):
        MacroTransport(
            http, monotonic=clock.monotonic, sleep=clock.sleep, jitter=lambda: 0
        )._request("GET", "https://example.test", max_bytes=20, deadline=100)
    assert count == calls


class Stream(httpx.SyncByteStream):
    def __init__(self, clock: Clock, oversized: bool) -> None:
        self.clock, self.oversized = clock, oversized
        self.closed = False
        self.consumed = 0

    def __iter__(self) -> Iterator[bytes]:
        self.consumed += 1
        yield b"["
        self.consumed += 1
        if not self.oversized:
            self.clock.elapsed = 100
        yield b"x" * 20 if self.oversized else b"]"
        self.consumed += 1
        yield b"more"

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "oversized,code", [(True, "invalid_payload"), (False, "deadline_exceeded")]
)
def test_stream_limits_stop_reading_and_close(oversized: bool, code: str) -> None:
    clock = Clock()
    stream = Stream(clock, oversized)
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        ) as http,
        pytest.raises(MacroError, match=f"^{code}$"),
    ):
        MacroTransport(http, monotonic=clock.monotonic)._request(
            "GET",
            "https://example.test",
            max_bytes=20,
            deadline=10,
        )
    assert stream.closed and stream.consumed == 2


def test_deadline_prevents_request_and_unaffordable_retry_sleep() -> None:
    clock, calls = Clock(), 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "20"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        transport = MacroTransport(http, monotonic=clock.monotonic, sleep=clock.sleep)
        for deadline in [0, 10]:
            with pytest.raises(MacroError, match="^deadline_exceeded$"):
                transport._request("GET", "https://example.test", max_bytes=20, deadline=deadline)
    assert calls == 1 and not clock.sleeps


def test_transport_failures_have_bounded_backoff_and_sanitized_errors() -> None:
    clock, calls = Clock(), 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("Private upstream details")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        pytest.raises(MacroError, match="^retry_exhausted$"),
    ):
        MacroTransport(
            http,
            request_interval=0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            jitter=lambda: 0,
        )._request("GET", "https://example.test", max_bytes=20, deadline=100)
    assert calls == 3 and clock.sleeps == [1, 2]


def test_invalid_http_content_encoding_is_sanitized_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=b"Private bad gzip"
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        pytest.raises(MacroError, match="^invalid_payload$"),
    ):
        MacroTransport(http, monotonic=lambda: 0)._request(
            "GET",
            "https://example.test",
            max_bytes=20,
            deadline=100,
        )
    assert calls == 1


def test_command_request_budget_counts_retries_and_multiple_fetches() -> None:
    clock, calls = Clock(), 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls == 1 else httpx.Response(200, content=b"ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        transport = MacroTransport(
            http,
            max_requests=2,
            request_interval=0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            jitter=lambda: 0,
        )
        assert (
            transport._request("GET", "https://example.test", max_bytes=20, deadline=100)[0]
            == b"ok"
        )
        with pytest.raises(MacroError, match="^retry_exhausted$"):
            transport._request("GET", "https://example.test", max_bytes=20, deadline=100)
    assert calls == transport.requests_used == 2


def test_exhausted_request_budget_prevents_retry_sleep_and_request() -> None:
    clock = Clock()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as http:
        transport = MacroTransport(
            http, max_requests=1, monotonic=clock.monotonic, sleep=clock.sleep
        )
        with pytest.raises(MacroError, match="^retry_exhausted$"):
            transport._request("GET", "https://example.test", max_bytes=20, deadline=100)
    assert transport.requests_used == 1 and not clock.sleeps
