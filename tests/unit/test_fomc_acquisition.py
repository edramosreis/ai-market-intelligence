"""Synthetic raw transport only; no source requests, artifact writes or database access."""

import gzip
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from market_intelligence.fomc import client as client_module
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.client import AcquisitionError, FomcAcquisitionClient

DOCUMENT_ID = PILOT_DOCUMENTS[0].document_id
BODY = b"<html><title>Synthetic statement</title><body>Test evidence</body></html>"


class Clock:
    elapsed = 0.0

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds

    def now(self) -> datetime:
        return datetime(2026, 10, 19, tzinfo=UTC) + timedelta(seconds=self.elapsed)


def response(
    status: int = 200, body: bytes = BODY, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"Content-Type": "text/html; charset=utf-8", **(headers or {})},
        stream=httpx.ByteStream(body),
    )


def acquisition(http: httpx.Client, clock: Clock, max_requests: int = 8) -> FomcAcquisitionClient:
    return FomcAcquisitionClient(
        http,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        now=clock.now,
        max_requests=max_requests,
    )


def test_exact_target_and_raw_receipts_ignore_inherited_http_settings() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(headers={"ETag": '"sample"', "Last-Modified": "cache hint only"})

    clock = Clock()
    with httpx.Client(
        transport=httpx.MockTransport(handler),
        auth=("private", "secret"),
        headers={"X-Private": "secret"},
        params={"key": "secret"},
        cookies={"private": "secret"},
        follow_redirects=True,
    ) as http:
        client = acquisition(http, clock)
        first = client.fetch(DOCUMENT_ID)
        replay = client.fetch(DOCUMENT_ID)
    assert str(calls[0].url) == PILOT_DOCUMENTS[0].url
    assert not {"authorization", "cookie", "x-private"} & set(calls[0].headers)
    assert calls[0].headers["accept-encoding"] == "gzip, identity"
    assert calls[0].extensions["timeout"] == {"connect": 5, "read": 20, "write": 20, "pool": 20}
    assert first.body == replay.body == BODY
    assert first.raw_sha256 == replay.raw_sha256 and first.raw_version_id == replay.raw_version_id
    assert first.receipt_id != replay.receipt_id
    assert first.received_at == first.fetch_started_at == clock.now() - timedelta(seconds=1)
    assert replay.fetch_started_at == clock.now()
    assert first.etag == '"sample"' and first.last_modified == "cache hint only"
    assert first.document.published_on.year == 2024 and first.received_at.year == 2026
    assert client.requests_used == 2 and client.bytes_used == 4 * len(BODY)
    assert clock.sleeps == [1]
    assert "Test evidence" not in repr(first)


@pytest.mark.parametrize(
    "status,code",
    [
        (301, "http_error"),
        (304, "http_error"),
        (404, "http_error"),
        (401, "source_rejected"),
        (403, "source_rejected"),
        (429, "source_rejected"),
    ],
)
def test_rejection_redirects_and_unsolicited_304_stop_without_retry(status: int, code: str) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response(
            status, headers={"Location": "https://other.test/private", "Retry-After": "1"}
        )

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        client = acquisition(http, Clock())
        with pytest.raises(AcquisitionError, match=f"^{code}$"):
            client.fetch(DOCUMENT_ID)
        with pytest.raises(AcquisitionError, match="^client_halted$"):
            client.fetch(PILOT_DOCUMENTS[1].document_id)
    assert len(calls) == 1


@pytest.mark.parametrize("transport_error", [False, True])
def test_transient_failure_has_two_attempts_and_sanitized_error(transport_error: bool) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if transport_error:
            raise httpx.ConnectError("private upstream text")
        return response(503)

    clock = Clock()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = acquisition(http, clock)
        with pytest.raises(AcquisitionError, match="^retry_exhausted$"):
            client.fetch(DOCUMENT_ID)
    assert calls == client.requests_used == 2 and clock.sleeps == [1]


def test_retry_success_counts_against_shared_command_budget() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response(502 if calls == 1 else 200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = acquisition(http, Clock(), max_requests=2)
        assert client.fetch(DOCUMENT_ID).body == BODY
        with pytest.raises(AcquisitionError, match="^request_limit$"):
            client.fetch(PILOT_DOCUMENTS[1].document_id)
    assert calls == 2


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes], after_first: Callable[[], None] = lambda: None) -> None:
        self.chunks, self.after_first = chunks, after_first
        self.closed, self.consumed = False, 0

    def __iter__(self) -> Iterator[bytes]:
        for index, chunk in enumerate(self.chunks):
            if index == 1:
                self.after_first()
            self.consumed += 1
            yield chunk

    def close(self) -> None:
        self.closed = True


def test_size_overflow_closes_stream_before_remaining_chunks() -> None:
    stream = Stream([b"x" * (2 * 1024 * 1024), b"x", b"unread"])
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"Content-Type": "text/html"}, stream=stream)
            )
        ) as http,
        pytest.raises(AcquisitionError, match="^size_limit$"),
    ):
        acquisition(http, Clock()).fetch(DOCUMENT_ID)
    assert stream.closed and stream.consumed == 2


def test_deadline_during_stream_stops_and_releases_response() -> None:
    clock = Clock()
    stream = Stream([b"first", b"second", b"unread"], lambda: setattr(clock, "elapsed", 120))
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"Content-Type": "text/html"}, stream=stream)
            )
        ) as http,
        pytest.raises(AcquisitionError, match="^deadline_exceeded$"),
    ):
        acquisition(http, clock).fetch(DOCUMENT_ID)
    assert stream.closed and stream.consumed == 2


def test_server_retry_after_halts_even_on_transient_status() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: response(503, headers={"Retry-After": "1"}))
    ) as http:
        client = acquisition(http, Clock())
        with pytest.raises(AcquisitionError, match="^source_rejected$"):
            client.fetch(DOCUMENT_ID)
    assert client.requests_used == 1


def test_elapsed_deadline_and_unknown_id_make_no_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response()

    clock = Clock()
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = acquisition(http, clock)
        with pytest.raises(ValueError, match="Unknown pilot document"):
            client.fetch("https://other.test/private")
        clock.elapsed = 120
        with pytest.raises(AcquisitionError, match="^deadline_exceeded$"):
            client.fetch(DOCUMENT_ID)
    assert calls == 0


@pytest.mark.parametrize(
    "body,headers,code",
    [
        (BODY, {"Content-Type": "application/pdf"}, "invalid_response"),
        (BODY, {"Content-Encoding": "br"}, "invalid_response"),
        (b"", {}, "invalid_response"),
        (BODY, {"Content-Length": "1"}, "invalid_response"),
        (BODY, {"Content-Length": "-1"}, "invalid_response"),
        (BODY, {"Content-Length": "9999999"}, "size_limit"),
    ],
)
def test_invalid_media_empty_and_misleading_length_rejected(
    body: bytes, headers: dict[str, str], code: str
) -> None:
    result = response(body=body, headers=headers)
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: result)) as http,
        pytest.raises(AcquisitionError, match=f"^{code}$"),
    ):
        acquisition(http, Clock()).fetch(DOCUMENT_ID)
    assert result.is_closed


def test_gzip_and_identity_encodings_have_the_same_raw_version() -> None:
    compressed = gzip.compress(BODY, mtime=0)
    replies = iter(
        [
            response(),
            response(
                body=compressed,
                headers={"Content-Encoding": "gzip", "Content-Length": str(len(compressed))},
            ),
        ]
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _: next(replies))) as http:
        client = acquisition(http, Clock())
        plain = client.fetch(DOCUMENT_ID)
        zipped = client.fetch(DOCUMENT_ID)
    assert zipped.body == plain.body == BODY
    assert zipped.raw_sha256 == plain.raw_sha256
    assert zipped.raw_version_id == plain.raw_version_id
    assert zipped.encoded_size == len(compressed) and zipped.content_encoding == "gzip"
    assert client.bytes_used == 3 * len(BODY) + len(compressed)


@pytest.mark.parametrize(
    "payload",
    [
        b"bad gzip",
        gzip.compress(BODY, mtime=0)[:-1],
        gzip.compress(BODY, mtime=0) + b"tail",
        gzip.compress(BODY, mtime=0) * 2,
    ],
)
def test_invalid_truncated_or_multiple_gzip_members_are_rejected(payload: bytes) -> None:
    result = response(body=payload, headers={"Content-Encoding": "gzip"})
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: result)) as http,
        pytest.raises(AcquisitionError, match="^invalid_response$"),
    ):
        acquisition(http, Clock()).fetch(DOCUMENT_ID)
    assert result.is_closed


def test_compressed_expansion_stops_before_following_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 128)
    compressed = gzip.compress(b"x" * 129, mtime=0)
    assert len(compressed) < 128
    stream = Stream([compressed, b"unread"])
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
                    stream=stream,
                )
            )
        ) as http,
        pytest.raises(AcquisitionError, match="^size_limit$"),
    ):
        acquisition(http, Clock()).fetch(DOCUMENT_ID)
    assert stream.closed and stream.consumed == 1


def test_aggregate_size_limit_includes_previous_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "MAX_TOTAL_BYTES", 4 * len(BODY) - 1)
    with httpx.Client(transport=httpx.MockTransport(lambda _: response())) as http:
        client = acquisition(http, Clock())
        assert client.fetch(DOCUMENT_ID).body == BODY
        with pytest.raises(AcquisitionError, match="^size_limit$"):
            client.fetch(PILOT_DOCUMENTS[1].document_id)
        with pytest.raises(AcquisitionError, match="^client_halted$"):
            client.fetch(PILOT_DOCUMENTS[2].document_id)
    assert client.requests_used == 2


def test_same_url_cannot_reset_attempt_budget_by_fetching_again() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: response())) as http:
        client = acquisition(http, Clock())
        client.fetch(DOCUMENT_ID)
        client.fetch(DOCUMENT_ID)
        with pytest.raises(AcquisitionError, match="^request_limit$"):
            client.fetch(DOCUMENT_ID)
    assert client.requests_used == 2


def test_changed_bytes_and_logical_document_have_separate_version_identities() -> None:
    changed = BODY.replace(b"Test evidence", b"Changed evidence")
    replies = iter([response(), response(body=changed), response()])
    with httpx.Client(transport=httpx.MockTransport(lambda _: next(replies))) as http:
        client = acquisition(http, Clock())
        initial = client.fetch(DOCUMENT_ID)
        update = client.fetch(DOCUMENT_ID)
        other = client.fetch(PILOT_DOCUMENTS[1].document_id)
    assert initial.document.document_id == update.document.document_id
    assert initial.raw_sha256 != update.raw_sha256
    assert initial.raw_version_id != update.raw_version_id
    assert initial.raw_sha256 == other.raw_sha256
    assert initial.raw_version_id != other.raw_version_id


def test_interrupted_read_closes_partial_stream_and_retries_from_empty_body() -> None:
    class InterruptedStream(Stream):
        def __iter__(self) -> Iterator[bytes]:
            yield BODY[:5]
            raise httpx.ReadError("private upstream failure")

    stream = InterruptedStream([])
    replies = iter(
        [
            httpx.Response(200, headers={"Content-Type": "text/html"}, stream=stream),
            response(),
        ]
    )
    with httpx.Client(transport=httpx.MockTransport(lambda _: next(replies))) as http:
        client = acquisition(http, Clock())
        result = client.fetch(DOCUMENT_ID)
    assert stream.closed and result.body == BODY
    assert client.requests_used == 2 and client.bytes_used == 2 * (len(BODY) + 5)


def test_spacing_that_cannot_fit_deadline_makes_no_second_request() -> None:
    clock = Clock()
    with httpx.Client(transport=httpx.MockTransport(lambda _: response())) as http:
        client = FomcAcquisitionClient(
            http,
            max_seconds=0.5,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        client.fetch(DOCUMENT_ID)
        with pytest.raises(AcquisitionError, match="^deadline_exceeded$"):
            client.fetch(PILOT_DOCUMENTS[1].document_id)
    assert client.requests_used == 1 and not clock.sleeps
