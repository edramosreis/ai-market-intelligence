"""Bounded synchronous transport shared only by the two direct macro clients."""

import math
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from market_intelligence.macro.models import MacroError, MacroErrorCode, utc


class MacroTransport:
    def __init__(
        self,
        client: httpx.Client,
        *,
        attempts: int = 3,
        request_interval: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise ValueError("Macro attempts must be between one and five")
        if not math.isfinite(request_interval) or request_interval < 0:
            raise ValueError("Invalid macro request interval")
        self.client, self.attempts, self.request_interval = client, attempts, request_interval
        self.monotonic, self.sleep, self.now, self.jitter = monotonic, sleep, now, jitter
        self.last_request: float | None = None

    def check_deadline(self, deadline: float) -> None:
        if not math.isfinite(deadline):
            raise ValueError("Macro deadline must be finite")
        if self.monotonic() >= deadline:
            raise MacroError(MacroErrorCode.DEADLINE_EXCEEDED)

    def _pause(self, seconds: float, deadline: float) -> None:
        if not math.isfinite(seconds) or seconds < 0 or self.monotonic() + seconds >= deadline:
            raise MacroError(MacroErrorCode.DEADLINE_EXCEEDED)
        if seconds:
            self.sleep(seconds)

    def _retry_after(self, raw: str | None) -> float | None:
        if raw is None:
            return None
        try:
            try:
                seconds = float(raw)
            except ValueError:
                seconds = (parsedate_to_datetime(raw) - utc(self.now())).total_seconds()
            return seconds if math.isfinite(seconds) and seconds >= 0 else None
        except ValueError, TypeError, OverflowError:
            return None

    def _request(
        self,
        method: str,
        url: str,
        *,
        max_bytes: int,
        deadline: float,
        json_body: dict[str, object] | None = None,
    ) -> tuple[bytes, datetime, datetime]:
        self.check_deadline(deadline)
        for attempt in range(self.attempts):
            if self.last_request is not None:
                self._pause(
                    max(0, self.request_interval - (self.monotonic() - self.last_request)), deadline
                )
            self.check_deadline(deadline)
            remaining = deadline - self.monotonic()
            self.last_request = self.monotonic()
            started = utc(self.now())
            retry_after: str | None = None
            try:
                with self.client.stream(
                    method,
                    url,
                    json=json_body,
                    follow_redirects=False,
                    timeout=httpx.Timeout(min(20, remaining), connect=min(5, remaining)),
                ) as response:
                    self.check_deadline(deadline)
                    if response.status_code not in {429, 500, 502, 503, 504}:
                        if response.status_code != 200:
                            raise MacroError(MacroErrorCode.HTTP_ERROR)
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            self.check_deadline(deadline)
                            if len(body) + len(chunk) > max_bytes:
                                raise MacroError(MacroErrorCode.INVALID_PAYLOAD)
                            body.extend(chunk)
                        self.check_deadline(deadline)
                        return bytes(body), started, utc(self.now())
                    retry_after = response.headers.get("retry-after")
            except httpx.DecodingError:
                raise MacroError(MacroErrorCode.INVALID_PAYLOAD) from None
            except httpx.TransportError:
                self.check_deadline(deadline)
            if attempt + 1 == self.attempts:
                raise MacroError(MacroErrorCode.RETRY_EXHAUSTED)
            delay = self._retry_after(retry_after)
            self._pause(
                delay if delay is not None else min(30, 2**attempt + self.jitter()), deadline
            )
        raise AssertionError("Retry loop must return or raise")
