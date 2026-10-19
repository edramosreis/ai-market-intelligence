"""Bounded raw HTML candidates; HTTP success does not validate policy-document content."""

import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

import httpx

from market_intelligence.documents.fomc import pilot_document
from market_intelligence.documents.models import DocumentSpec, identity

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_REQUESTS = 8
MAX_ATTEMPTS_PER_URL = 2
MAX_SECONDS = 120
TRANSIENT_STATUSES = {500, 502, 503, 504}


class AcquisitionErrorCode(StrEnum):
    INVALID_RESPONSE = "invalid_response"
    SOURCE_REJECTED = "source_rejected"
    HTTP_ERROR = "http_error"
    RETRY_EXHAUSTED = "retry_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    REQUEST_LIMIT = "request_limit"
    SIZE_LIMIT = "size_limit"
    CLIENT_HALTED = "client_halted"


class AcquisitionError(Exception):
    def __init__(self, code: AcquisitionErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AcquisitionError(AcquisitionErrorCode.INVALID_RESPONSE)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class FetchedDocument:
    """Unvalidated in-memory original, distinct from future accepted/stored versions."""

    document: DocumentSpec
    body: bytes = field(repr=False)
    receipt_id: UUID
    fetch_started_at: datetime
    received_at: datetime
    content_type: str
    content_encoding: str
    encoded_size: int
    etag: str | None
    last_modified: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fetch_started_at", _utc(self.fetch_started_at))
        object.__setattr__(self, "received_at", _utc(self.received_at))
        if self.received_at < self.fetch_started_at:
            raise AcquisitionError(AcquisitionErrorCode.INVALID_RESPONSE)

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    @property
    def raw_version_id(self) -> str:
        return identity("raw-html-v1", self.document.document_id, self.raw_sha256)


def _header(value: str | None, limit: int = 1024) -> str | None:
    # Keep cache hints only when bounded and printable; never interpret them as publication.
    if value is None or not 1 <= len(value) <= limit or any(not 32 <= ord(c) < 127 for c in value):
        return None
    return value


class FomcAcquisitionClient:
    """One sequential command budget, shared by every attempt, retry and document read."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        max_requests: int = MAX_REQUESTS,
        max_seconds: float = MAX_SECONDS,
        request_interval: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        receipt_id: Callable[[], UUID] = uuid4,
    ) -> None:
        if type(max_requests) is not int or not 1 <= max_requests <= MAX_REQUESTS:
            raise ValueError("Request budget must be between one and eight")
        if not math.isfinite(max_seconds) or not 0 < max_seconds <= MAX_SECONDS:
            raise ValueError("Command budget must be positive and at most 120 seconds")
        if not math.isfinite(request_interval) or request_interval < 1:
            raise ValueError("Request spacing must be at least one second")
        self.client, self.max_requests = client, max_requests
        self.monotonic, self.sleep, self.now, self.receipt_id = monotonic, sleep, now, receipt_id
        self.deadline = monotonic() + max_seconds
        self.request_interval = request_interval
        self.requests_used = 0
        self.bytes_used = 0
        self._attempts: dict[str, int] = {}
        self._last_request: float | None = None
        self._halted = False

    def _check_deadline(self) -> None:
        if self.monotonic() >= self.deadline:
            raise AcquisitionError(AcquisitionErrorCode.DEADLINE_EXCEEDED)

    def _before_request(self, url: str) -> None:
        self._check_deadline()
        if (
            self.requests_used >= self.max_requests
            or self._attempts.get(url, 0) >= MAX_ATTEMPTS_PER_URL
        ):
            raise AcquisitionError(AcquisitionErrorCode.REQUEST_LIMIT)
        if self._last_request is not None:
            pause = max(0, self.request_interval - (self.monotonic() - self._last_request))
            if self.monotonic() + pause >= self.deadline:
                raise AcquisitionError(AcquisitionErrorCode.DEADLINE_EXCEEDED)
            if pause:
                self.sleep(pause)
        self._check_deadline()
        self._last_request = self.monotonic()
        self.requests_used += 1
        self._attempts[url] = self._attempts.get(url, 0) + 1

    def _count_bytes(self, count: int) -> None:
        self.bytes_used += count
        if self.bytes_used > MAX_TOTAL_BYTES:
            raise AcquisitionError(AcquisitionErrorCode.SIZE_LIMIT)

    def _read(self, response: httpx.Response) -> tuple[bytes, int, str]:
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding != "identity":
            raise AcquisitionError(AcquisitionErrorCode.INVALID_RESPONSE)
        length = response.headers.get("content-length")
        if length is not None:
            if not length.isascii() or not length.isdecimal() or len(length) > 10:
                raise AcquisitionError(AcquisitionErrorCode.INVALID_RESPONSE)
            if int(length) > MAX_RESPONSE_BYTES:
                raise AcquisitionError(AcquisitionErrorCode.SIZE_LIMIT)
        body = bytearray()
        for chunk in response.iter_raw():
            self._check_deadline()
            self._count_bytes(2 * len(chunk))  # Identity encoding: wire and entity counts.
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise AcquisitionError(AcquisitionErrorCode.SIZE_LIMIT)
            body.extend(chunk)
        self._check_deadline()
        if not body or (length is not None and int(length) != len(body)):
            raise AcquisitionError(AcquisitionErrorCode.INVALID_RESPONSE)
        return bytes(body), len(body), encoding

    def fetch(self, document_id: str) -> FetchedDocument:
        document = pilot_document(document_id)  # No URL input or implicit discovery.
        if self._halted:
            raise AcquisitionError(AcquisitionErrorCode.CLIENT_HALTED)
        try:
            return self._fetch(document)
        except AcquisitionError:
            self._halted = True
            raise

    def _fetch(self, document: DocumentSpec) -> FetchedDocument:
        for _ in range(MAX_ATTEMPTS_PER_URL):
            self._before_request(document.url)
            started = _utc(self.now())
            remaining = self.deadline - self.monotonic()
            # A fresh Request bypasses inherited client auth, cookies, headers and query params.
            request = httpx.Request(
                "GET",
                document.url,
                headers={
                    "Accept": "text/html",
                    "Accept-Encoding": "identity",
                    "User-Agent": "ai-market-intelligence-fomc/0.1",
                },
                extensions={
                    "timeout": httpx.Timeout(
                        min(20, remaining), connect=min(5, remaining)
                    ).as_dict()
                },
            )
            try:
                response = self.client.send(request, stream=True, follow_redirects=False, auth=None)
                try:
                    self._check_deadline()
                    if response.status_code in {401, 403, 429} or (
                        response.status_code in TRANSIENT_STATUSES
                        and "retry-after" in response.headers
                    ):
                        # Explicit backpressure stops the command; never guess a reset time.
                        raise AcquisitionError(AcquisitionErrorCode.SOURCE_REJECTED)
                    if response.status_code in TRANSIENT_STATUSES:
                        continue
                    if response.status_code != 200:
                        raise AcquisitionError(AcquisitionErrorCode.HTTP_ERROR)
                    content_type = _header(response.headers.get("content-type"), 256)
                    if (
                        content_type is None
                        or content_type.split(";", 1)[0].strip().lower() != "text/html"
                    ):
                        raise AcquisitionError(AcquisitionErrorCode.INVALID_RESPONSE)
                    body, encoded_size, encoding = self._read(response)
                    return FetchedDocument(
                        document,
                        body,
                        self.receipt_id(),
                        started,
                        _utc(self.now()),
                        content_type,
                        encoding,
                        encoded_size,
                        _header(response.headers.get("etag")),
                        _header(response.headers.get("last-modified")),
                    )
                finally:
                    response.close()
            except httpx.TransportError:
                # No raw upstream error text, partial body or headers escape on failure.
                continue
        raise AcquisitionError(AcquisitionErrorCode.RETRY_EXHAUSTED)
