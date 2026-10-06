"""Synthetic Treasury XML, native units/missingness, and bounded HTTP behavior."""

import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from xml.sax.saxutils import escape

import httpx
import pytest

from market_intelligence.treasury.client import (
    MAX_RESPONSE_BYTES,
    TreasuryClient,
    parse_treasury_xml,
)
from market_intelligence.treasury.models import (
    TreasuryCurve,
    TreasuryError,
    TreasuryErrorCode,
    TreasuryMissingReason,
    TreasuryMonth,
    TreasuryRate,
    TreasuryTenor,
)

MONTH = TreasuryMonth(2024, 1)
NAMESPACES = (
    'xmlns="http://www.w3.org/2005/Atom" '
    'xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" '
    'xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"'
)


def entry(day: str = "2024-01-02", fields: str = "") -> str:
    return (
        f"<entry><content><m:properties><d:NEW_DATE>{escape(day)}T00:00:00</d:NEW_DATE>"
        f"{fields}</m:properties></content></entry>"
    )


def feed(entries: str = "", extra: str = "") -> bytes:
    return f"<feed {NAMESPACES}>{extra}{entries}</feed>".encode()


VALID = feed(entry(fields="<d:BC_2YEAR>4.2500</d:BC_2YEAR><d:BC_10YEAR>4.125</d:BC_10YEAR>"))


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
        return datetime(2024, 1, 1, tzinfo=UTC) + timedelta(seconds=self.elapsed)


def test_native_dates_units_maturity_order_and_missing_reasons() -> None:
    fields = (
        "<d:BC_2YEAR>4.250000000000000001</d:BC_2YEAR><d:BC_10YEAR>0</d:BC_10YEAR>"
        '<d:BC_1_5MONTH>4.45</d:BC_1_5MONTH><d:BC_20YEAR m:null="true"/>'
        '<d:BC_30YEAR m:null="true"/><d:BC_30YEARDISPLAY>8.5</d:BC_30YEARDISPLAY>'
    )
    curves = parse_treasury_xml(feed(entry("2024-01-03", fields) + entry(fields=fields)), MONTH)
    assert [curve.observed_on for curve in curves] == [date(2024, 1, 2), date(2024, 1, 3)]
    rates = {rate.tenor: rate for rate in curves[0].rates}
    assert tuple(rates) == tuple(TreasuryTenor)
    assert rates[TreasuryTenor.TWO_YEARS].yield_percent == Decimal("4.250000000000000001")
    assert rates[TreasuryTenor.TEN_YEARS].yield_percent == Decimal(0)
    assert rates[TreasuryTenor.TEN_YEARS].missing_reason is None
    assert rates[TreasuryTenor.ONE_AND_HALF_MONTHS].yield_percent == Decimal("4.45")
    assert rates[TreasuryTenor.ONE_MONTH].missing_reason == TreasuryMissingReason.FIELD_ABSENT
    assert rates[TreasuryTenor.TWENTY_YEARS].missing_reason == TreasuryMissingReason.SOURCE_NULL
    assert rates[TreasuryTenor.THIRTY_YEARS].yield_percent is None  # no display-field fallback


def test_empty_month_is_no_observations_not_an_invented_calendar() -> None:
    assert parse_treasury_xml(feed(), MONTH) == []


@pytest.mark.parametrize("year,month", [(1989, 1), (True, 1), (2024, 0), (2024, 13), (2024, True)])
def test_invalid_months_are_rejected(year: int, month: int) -> None:
    with pytest.raises(ValueError):
        TreasuryMonth(year, month)


def test_month_boundaries_include_leap_years_and_december() -> None:
    assert TreasuryMonth(2024, 2).end == date(2024, 3, 1)
    assert TreasuryMonth(2024, 12).end == date(2025, 1, 1)
    assert MONTH.provider_month == "202401"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-0.01", "1e20", "0.0000000000000000001"])
def test_domain_precision_and_nominal_floor(value: str) -> None:
    with pytest.raises(ValueError):
        TreasuryRate(TreasuryTenor.TWO_YEARS, Decimal(value))


def test_domain_missing_values_and_date_identity_are_enforced() -> None:
    with pytest.raises(ValueError):
        TreasuryRate(TreasuryTenor.TWO_YEARS, None)
    with pytest.raises(ValueError):
        TreasuryRate(TreasuryTenor.TWO_YEARS, Decimal(1), TreasuryMissingReason.SOURCE_NULL)
    rates = parse_treasury_xml(VALID, MONTH)[0].rates
    with pytest.raises(ValueError):
        TreasuryCurve(datetime(2024, 1, 2, tzinfo=UTC), rates)
    with pytest.raises(ValueError):
        TreasuryCurve(date(2024, 1, 2), rates[:-1])


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "4.2%", "", "1e2", "1_000", "1,000"])
def test_malformed_or_negative_wire_rates(value: str) -> None:
    with pytest.raises(TreasuryError) as caught:
        parse_treasury_xml(feed(entry(fields=f"<d:BC_2YEAR>{escape(value)}</d:BC_2YEAR>")), MONTH)
    assert caught.value.code == TreasuryErrorCode.INVALID_PAYLOAD


@pytest.mark.parametrize(
    "payload",
    [
        b"<html>upstream error</html>",
        b"<!DOCTYPE feed [<!ENTITY x '4.0'>]>" + VALID,
        VALID.decode().encode("utf-16"),
        b"x" * (MAX_RESPONSE_BYTES + 1),
        feed(
            entry(fields="<d:BC_2YEAR>4</d:BC_2YEAR>"),
            '<link rel="next" href="https://example.invalid"/>',
        ),
        feed(entry(fields="<d:BC_2YEAR>4</d:BC_2YEAR>") * 2),
        feed(entry("2024-02-01", "<d:BC_2YEAR>4</d:BC_2YEAR>")),
        feed(entry(fields="<d:BC_2YEAR>4</d:BC_2YEAR><d:BC_2YEAR>5</d:BC_2YEAR>")),
        feed(entry(fields="<d:BC_99YEAR>4</d:BC_99YEAR>")),
        feed(entry(fields='<d:BC_2YEAR m:null="true">4</d:BC_2YEAR>')),
        feed(entry(fields='<d:BC_2YEAR m:null="unknown">4</d:BC_2YEAR>')),
        feed(entry(fields="<d:BC_2YEAR><x>4</x></d:BC_2YEAR>")),
        feed(entry()),
        VALID.replace(b"T00:00:00", b"T00:00:00Z"),
        VALID.replace(b"<m:properties>", b"<properties>").replace(
            b"</m:properties>", b"</properties>"
        ),
    ],
    ids=[
        "html",
        "doctype",
        "utf16",
        "oversized",
        "pagination",
        "duplicate-date",
        "outside-month",
        "duplicate-field",
        "unknown-tenor",
        "null-with-value",
        "invalid-null",
        "nested-rate",
        "no-rates",
        "timestamp-instead-of-date",
        "namespace",
    ],
)
def test_invalid_xml_contracts_are_sanitized(payload: bytes) -> None:
    with pytest.raises(TreasuryError) as caught:
        parse_treasury_xml(payload, MONTH)
    assert str(caught.value) == "invalid_payload"


def test_fixed_endpoint_query_timeout_and_pacing() -> None:
    clock = Clock()
    seen: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "home.treasury.gov"
        assert dict(request.url.params) == {
            "data": "daily_treasury_yield_curve",
            "field_tdr_date_value_month": "202401",
        }
        assert request.extensions["timeout"]["connect"] == 5
        return httpx.Response(200, content=VALID)

    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        source = TreasuryClient(http, monotonic=clock.monotonic, sleep=clock.sleep)
        assert len(source.fetch_month(MONTH, deadline=30)) == 1
        source.fetch_month(MONTH, deadline=30)
    assert len(seen) == 2 and clock.sleeps == [1]


@pytest.mark.parametrize(
    "retry_after,expected",
    [("2", 2), ("invalid", 1), ("nan", 1), ("Mon, 01 Jan 2024 00:00:03 GMT", 3)],
)
def test_retries_throttling_and_retry_after(retry_after: str, expected: float) -> None:
    clock = Clock()
    replies = iter(
        [
            httpx.Response(429, headers={"Retry-After": retry_after}),
            httpx.Response(200, content=VALID),
        ]
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _: next(replies))) as http:
        source = TreasuryClient(
            http,
            request_interval=0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            now=clock.now,
            jitter=lambda: 0,
        )
        assert source.fetch_month(MONTH, deadline=20)
    assert clock.sleeps == [expected]


def test_network_failure_is_retried_without_exposing_exception_text() -> None:
    clock = Clock()
    calls = 0

    def reply(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("private upstream details", request=request)
        return httpx.Response(200, content=VALID)

    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        source = TreasuryClient(
            http, monotonic=clock.monotonic, sleep=clock.sleep, jitter=lambda: 0
        )
        assert source.fetch_month(MONTH, deadline=20)
    assert calls == 2


@pytest.mark.parametrize(
    "status,code,calls",
    [(503, "retry_exhausted", 3), (403, "http_error", 1), (302, "http_error", 1)],
)
def test_retry_exhaustion_and_nonretryable_errors(status: int, code: str, calls: int) -> None:
    clock = Clock()
    seen = 0

    def reply(_: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        return httpx.Response(status, headers={"Location": "https://example.invalid"})

    with httpx.Client(transport=httpx.MockTransport(reply), follow_redirects=True) as http:
        source = TreasuryClient(
            http, monotonic=clock.monotonic, sleep=clock.sleep, jitter=lambda: 0
        )
        with pytest.raises(TreasuryError, match=code):
            source.fetch_month(MONTH, deadline=20)
    assert seen == calls


def test_response_and_deadline_limits_stop_before_another_request() -> None:
    clock = Clock()
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
            )
        ) as http,
        pytest.raises(TreasuryError, match="invalid_payload"),
    ):
        TreasuryClient(http).fetch_month(MONTH, deadline=time.monotonic() + 30)
    seen = 0

    def slow(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        clock.elapsed = 31
        return httpx.Response(200, content=VALID)

    with httpx.Client(transport=httpx.MockTransport(slow)) as http:
        source = TreasuryClient(http, monotonic=clock.monotonic, sleep=clock.sleep)
        with pytest.raises(TreasuryError, match="deadline_exceeded"):
            source.fetch_month(MONTH, deadline=30)
    assert seen == 1


def test_retry_after_cannot_exceed_deadline_and_invalid_constructor_is_rejected() -> None:
    clock = Clock()
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "100"}))
    ) as http:
        source = TreasuryClient(http, monotonic=clock.monotonic, sleep=clock.sleep)
        with pytest.raises(TreasuryError, match="deadline_exceeded"):
            source.fetch_month(MONTH, deadline=20)
        for attempts in (0, 6, True):
            with pytest.raises(ValueError):
                TreasuryClient(http, attempts=attempts)
        with pytest.raises(ValueError):
            TreasuryClient(http, request_interval=float("nan"))
        with pytest.raises(ValueError):
            source.fetch_month(MONTH, deadline=float("inf"))
    assert not clock.sleeps


def test_stream_deadline_closes_response_without_retrying() -> None:
    clock = Clock()
    closed = False
    calls = 0

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b" " * 65536
            clock.elapsed = 31
            yield VALID

        def close(self) -> None:
            nonlocal closed
            closed = True

    def reply(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=SlowStream())

    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        source = TreasuryClient(http, monotonic=clock.monotonic, sleep=clock.sleep)
        with pytest.raises(TreasuryError, match="deadline_exceeded"):
            source.fetch_month(MONTH, deadline=30)
    assert closed and calls == 1


def test_transport_timeout_past_deadline_reports_deadline_not_retry_exhaustion() -> None:
    clock = Clock()

    def reply(request: httpx.Request) -> httpx.Response:
        clock.elapsed = 31
        raise httpx.ReadTimeout("private details", request=request)

    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        source = TreasuryClient(http, attempts=1, monotonic=clock.monotonic, sleep=clock.sleep)
        with pytest.raises(TreasuryError, match="deadline_exceeded"):
            source.fetch_month(MONTH, deadline=30)


def test_small_stream_chunks_stop_at_deadline_without_consuming_the_rest() -> None:
    clock = Clock()
    consumed = 0
    closed = False
    calls = 0

    class TrickleStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            nonlocal consumed
            for _ in range(100):
                clock.elapsed += 1
                consumed += 1
                yield b" "
            yield VALID

        def close(self) -> None:
            nonlocal closed
            closed = True

    def reply(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=TrickleStream())

    with httpx.Client(transport=httpx.MockTransport(reply)) as http:
        source = TreasuryClient(http, monotonic=clock.monotonic, sleep=clock.sleep)
        with pytest.raises(TreasuryError, match="deadline_exceeded"):
            source.fetch_month(MONTH, deadline=30)
    assert consumed == 30 and clock.elapsed == 30 and closed and calls == 1


def test_stream_byte_limit_closes_response_before_consuming_the_rest() -> None:
    consumed = 0
    closed = False

    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            nonlocal consumed
            for _ in range(100):
                consumed += 1
                yield b" " * 65536

        def close(self) -> None:
            nonlocal closed
            closed = True

    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=OversizedStream()))
        ) as http,
        pytest.raises(TreasuryError, match="invalid_payload"),
    ):
        TreasuryClient(http).fetch_month(MONTH, deadline=time.monotonic() + 30)
    assert consumed == MAX_RESPONSE_BYTES // 65536 + 1 and closed
