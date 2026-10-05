"""Coinbase Exchange public wire contract with bounded, injectable retry behavior."""

import json
import math
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from market_intelligence.ingestion.models import (
    INTERVAL,
    Candle,
    ErrorCode,
    IngestionError,
    TimeWindow,
    utc_now,
)

BASE_URL = "https://api.exchange.coinbase.com"
PRODUCT_ID = "BTC-USD"


def reject_constant(value: str) -> None:
    raise ValueError("Nonstandard JSON number")


def decimal_value(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        raise IngestionError(ErrorCode.INVALID_CANDLE)
    try:
        return Decimal(value)
    except InvalidOperation:
        raise IngestionError(ErrorCode.INVALID_CANDLE) from None


def parse_candles(payload: object, window: TimeWindow, cutoff: datetime) -> list[Candle]:
    if not isinstance(payload, list) or len(payload) > 300:
        raise IngestionError(ErrorCode.INVALID_PAYLOAD)
    result: dict[datetime, Candle] = {}
    for row in payload:
        if not isinstance(row, list) or len(row) != 6:
            raise IngestionError(ErrorCode.INVALID_PAYLOAD)
        epoch = row[0]
        if isinstance(epoch, bool) or not isinstance(epoch, int):
            raise IngestionError(ErrorCode.INVALID_CANDLE)
        try:
            instant = datetime.fromtimestamp(epoch, UTC)
        except ValueError, OSError, OverflowError:
            raise IngestionError(ErrorCode.INVALID_CANDLE) from None
        if not window.start <= instant < window.end or instant + INTERVAL > cutoff:
            continue
        # Coinbase order: time, low, high, open, close, volume. Volume is base units.
        candle = Candle(instant, *(decimal_value(row[i]) for i in (3, 2, 1, 4, 5)))
        if instant in result and result[instant] != candle:
            raise IngestionError(ErrorCode.INVALID_CANDLE)
        result[instant] = candle
    return sorted(result.values(), key=lambda candle: candle.opened_at)


class CoinbaseClient:
    def __init__(
        self,
        client: httpx.Client,
        *,
        request_interval: float = 0.5,
        attempts: int = 5,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if not math.isfinite(request_interval) or request_interval < 0 or attempts < 1:
            raise ValueError("Invalid request interval or attempt limit")
        self.client = client
        self.request_interval = request_interval
        self.attempts = attempts
        self.monotonic = monotonic
        self.sleep = sleep
        self.jitter = jitter
        self.now = now
        self.last_request: float | None = None

    def pause(self, seconds: float, deadline: float) -> None:
        if self.monotonic() + seconds >= deadline:
            raise IngestionError(ErrorCode.DEADLINE_EXCEEDED)
        if seconds > 0:
            self.sleep(seconds)

    def retry_after(self, response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if raw is None:
            return None
        try:
            seconds = float(raw)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(raw) - self.now()).total_seconds()
            except ValueError, TypeError, OverflowError:
                return None
        return max(0, seconds) if math.isfinite(seconds) and seconds >= 0 else None

    def get_json(self, path: str, params: dict[str, str], deadline: float) -> Any:
        for attempt in range(self.attempts):
            if self.last_request is not None:
                self.pause(
                    max(0, self.request_interval - (self.monotonic() - self.last_request)), deadline
                )
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise IngestionError(ErrorCode.DEADLINE_EXCEEDED)
            self.last_request = self.monotonic()
            response: httpx.Response | None = None
            try:
                response = self.client.get(
                    path,
                    params=params,
                    timeout=httpx.Timeout(min(20, remaining), connect=min(5, remaining)),
                )
                if self.monotonic() >= deadline:
                    raise IngestionError(ErrorCode.DEADLINE_EXCEEDED)
            except httpx.TransportError:
                pass
            if response is not None and response.status_code not in {429, 500, 502, 503, 504}:
                if response.status_code != 200:
                    raise IngestionError(ErrorCode.HTTP_ERROR)
                try:
                    if len(response.content) > 2_000_000:
                        raise ValueError("Oversized response")
                    return json.loads(
                        response.content, parse_float=Decimal, parse_constant=reject_constant
                    )
                except ValueError, UnicodeDecodeError:
                    raise IngestionError(ErrorCode.INVALID_PAYLOAD) from None
            if attempt + 1 == self.attempts:
                raise IngestionError(ErrorCode.RETRY_EXHAUSTED)
            wait = self.retry_after(response) if response is not None else None
            self.pause(wait if wait is not None else min(30, 2**attempt + self.jitter()), deadline)
        raise AssertionError("Retry loop must return or raise")

    def verify_product(self, deadline: float) -> None:
        product = self.get_json(f"/products/{PRODUCT_ID}", {}, deadline)
        if (
            not isinstance(product, dict)
            or (product.get("id"), product.get("base_currency"), product.get("quote_currency"))
            != (PRODUCT_ID, "BTC", "USD")
            or product.get("margin_enabled") is not False
        ):
            raise IngestionError(ErrorCode.PRODUCT_MISMATCH)

    def fetch_chunk(self, window: TimeWindow, cutoff: datetime, deadline: float) -> list[Candle]:
        result: dict[datetime, Candle] = {}
        start = window.start
        while start < window.end:
            end = min(start + 250 * INTERVAL, window.end)
            page = TimeWindow(start, end)
            payload = self.get_json(
                f"/products/{PRODUCT_ID}/candles",
                {"granularity": "300", "start": start.isoformat(), "end": end.isoformat()},
                deadline,
            )
            for candle in parse_candles(payload, page, cutoff):
                previous = result.get(candle.opened_at)
                if previous is not None and previous != candle:
                    raise IngestionError(ErrorCode.INVALID_CANDLE)
                result[candle.opened_at] = candle
            start = end
        return sorted(result.values(), key=lambda candle: candle.opened_at)
