"""Bounded public Info reads; inclusive pagination preserves exact source milliseconds."""

import json
import math
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID, uuid4

import httpx

from market_intelligence.hyperliquid.models import (
    COIN,
    EPOCH,
    FundingEvent,
    FundingWindow,
    HyperliquidError,
    HyperliquidErrorCode,
    OpenInterestSnapshot,
    milliseconds,
    utc,
)

INFO_URL = "https://api.hyperliquid.xyz/info"
MAX_RESPONSE_BYTES = 2_000_000
PAGE_LIMIT = 500
MAX_FUNDING_PAGES = 4


def _now() -> datetime:
    return datetime.now(UTC)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON property")
        result[name] = value
    return result


def _json(payload: bytes) -> Any:
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("Oversized JSON")
    return json.loads(payload.decode("utf-8"), object_pairs_hook=_unique)


def _decimal(value: Any) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        raise ValueError("Expected a decimal string")
    return Decimal(value)


def parse_funding(payload: bytes, window: FundingWindow) -> list[FundingEvent]:
    try:
        rows = _json(payload)
        if not isinstance(rows, list) or len(rows) > PAGE_LIMIT:
            raise ValueError("Unexpected funding page")
        result: list[FundingEvent] = []
        slots: set[datetime] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"coin", "time", "fundingRate", "premium"}:
                raise ValueError("Unexpected funding fields")
            if row["coin"] != COIN or type(row["time"]) is not int:
                raise ValueError("Wrong instrument or event time")
            instant = EPOCH + timedelta(milliseconds=row["time"])
            event = FundingEvent(instant, _decimal(row["fundingRate"]), _decimal(row["premium"]))
            if (
                not window.start <= instant < window.end
                or (result and instant <= result[-1].event_at)
                or event.settlement_hour in slots
            ):
                raise ValueError("Unordered, duplicate, or out-of-window funding event")
            slots.add(event.settlement_hour)
            result.append(event)
        return result
    except ValueError, TypeError, OverflowError, RecursionError:
        raise HyperliquidError(HyperliquidErrorCode.INVALID_PAYLOAD) from None


def parse_open_interest(
    payload: bytes, *, snapshot_id: UUID, fetch_started_at: datetime, received_at: datetime
) -> OpenInterestSnapshot:
    try:
        rows = _json(payload)
        if not isinstance(rows, list) or len(rows) != 2 or not isinstance(rows[0], dict):
            raise ValueError("Unexpected contexts envelope")
        meta, contexts = rows
        universe = meta.get("universe")
        if (
            not isinstance(universe, list)
            or not isinstance(contexts, list)
            or not 1 <= len(universe) <= 10000
            or len(universe) != len(contexts)
            or any(
                not isinstance(asset, dict) or not isinstance(asset.get("name"), str)
                for asset in universe
            )
        ):
            raise ValueError("Invalid universe/context mapping")
        names = [asset["name"] for asset in universe]
        if len(set(names)) != len(names) or names.count(COIN) != 1:
            raise ValueError("Ambiguous BTC context")
        index = names.index(COIN)
        if universe[index].get("isDelisted", False) is not False:
            raise ValueError("BTC contract is unavailable")
        # First native perp dex, collateral token 0 (USDC); refuse a changed mapping.
        if "collateralToken" in meta and (
            type(meta["collateralToken"]) is not int or meta["collateralToken"] != 0
        ):
            raise ValueError("Unexpected collateral")
        ctx = contexts[index]
        if not isinstance(ctx, dict):
            raise ValueError("Invalid BTC context")
        return OpenInterestSnapshot(
            snapshot_id,
            fetch_started_at,
            received_at,
            _decimal(ctx["openInterest"]),
            _decimal(ctx["markPx"]),
            _decimal(ctx["oraclePx"]),
        )
    except ValueError, TypeError, KeyError, RecursionError:
        raise HyperliquidError(HyperliquidErrorCode.INVALID_PAYLOAD) from None


class HyperliquidClient:
    def __init__(
        self,
        client: httpx.Client,
        *,
        attempts: int = 3,
        request_interval: float = 3.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = _now,
        jitter: Callable[[], float] = random.random,
        snapshot_id: Callable[[], UUID] = uuid4,
    ) -> None:
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise ValueError("Hyperliquid attempts must be between one and five")
        if not math.isfinite(request_interval) or request_interval < 0:
            raise ValueError("Invalid Hyperliquid request interval")
        self.client, self.attempts, self.request_interval = client, attempts, request_interval
        self.monotonic, self.sleep, self.now, self.jitter = monotonic, sleep, now, jitter
        self.snapshot_id = snapshot_id
        self.last_request: float | None = None

    def _pause(self, seconds: float, deadline: float) -> None:
        if not math.isfinite(seconds) or seconds < 0 or self.monotonic() + seconds >= deadline:
            raise HyperliquidError(HyperliquidErrorCode.DEADLINE_EXCEEDED)
        if seconds:
            self.sleep(seconds)

    def _retry_after(self, raw: str | None) -> float | None:
        if raw is None:
            return None
        try:
            try:
                seconds = float(raw)
            except ValueError:
                seconds = (parsedate_to_datetime(raw) - self.now()).total_seconds()
            return seconds if math.isfinite(seconds) and seconds >= 0 else None
        except ValueError, TypeError, OverflowError:
            return None

    def _post(
        self, request: dict[str, Any], *, deadline: float
    ) -> tuple[bytes, datetime, datetime]:
        if not math.isfinite(deadline):
            raise ValueError("Hyperliquid deadline must be finite")
        for attempt in range(self.attempts):
            if self.last_request is not None:
                self._pause(
                    max(0, self.request_interval - (self.monotonic() - self.last_request)), deadline
                )
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise HyperliquidError(HyperliquidErrorCode.DEADLINE_EXCEEDED)
            self.last_request = self.monotonic()
            started = utc(self.now())
            retry_after: str | None = None
            try:
                with self.client.stream(
                    "POST",
                    INFO_URL,
                    json=request,
                    follow_redirects=False,
                    timeout=httpx.Timeout(min(20, remaining), connect=min(5, remaining)),
                ) as response:
                    if self.monotonic() >= deadline:
                        raise HyperliquidError(HyperliquidErrorCode.DEADLINE_EXCEEDED)
                    if response.status_code not in {429, 500, 502, 503, 504}:
                        if response.status_code != 200:
                            raise HyperliquidError(HyperliquidErrorCode.HTTP_ERROR)
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            if self.monotonic() >= deadline:
                                raise HyperliquidError(HyperliquidErrorCode.DEADLINE_EXCEEDED)
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise HyperliquidError(HyperliquidErrorCode.INVALID_PAYLOAD)
                            body.extend(chunk)
                        if self.monotonic() >= deadline:
                            raise HyperliquidError(HyperliquidErrorCode.DEADLINE_EXCEEDED)
                        return bytes(body), started, utc(self.now())
                    retry_after = response.headers.get("retry-after")
            except httpx.TransportError:
                if self.monotonic() >= deadline:
                    raise HyperliquidError(HyperliquidErrorCode.DEADLINE_EXCEEDED) from None
            if attempt + 1 == self.attempts:
                raise HyperliquidError(HyperliquidErrorCode.RETRY_EXHAUSTED)
            delay = self._retry_after(retry_after)
            self._pause(
                delay if delay is not None else min(30, 2**attempt + self.jitter()), deadline
            )
        raise AssertionError("Retry loop must return or raise")

    def fetch_funding(self, window: FundingWindow, *, deadline: float) -> list[FundingEvent]:
        result: list[FundingEvent] = []
        start = window.start
        for _ in range(MAX_FUNDING_PAGES):
            payload, _, _ = self._post(
                {
                    "type": "fundingHistory",
                    "coin": COIN,
                    "startTime": milliseconds(start),
                    "endTime": milliseconds(window.end) - 1,
                },
                deadline=deadline,
            )
            page = parse_funding(payload, FundingWindow(start, window.end))
            new = page
            if result and page and page[0].event_at == result[-1].event_at:
                if page[0] != result[-1]:
                    raise HyperliquidError(HyperliquidErrorCode.INVALID_PAYLOAD)
                new = page[1:]
            if new and result and new[0].settlement_hour <= result[-1].settlement_hour:
                raise HyperliquidError(HyperliquidErrorCode.INVALID_PAYLOAD)
            result.extend(new)
            if self.monotonic() >= deadline:
                raise HyperliquidError(HyperliquidErrorCode.DEADLINE_EXCEEDED)
            if len(page) < PAGE_LIMIT:
                return result
            if not new:
                raise HyperliquidError(HyperliquidErrorCode.INVALID_PAYLOAD)
            start = result[-1].event_at
        raise HyperliquidError(HyperliquidErrorCode.INVALID_PAYLOAD)

    def fetch_open_interest(self, *, deadline: float) -> OpenInterestSnapshot:
        payload, started, received = self._post(
            {"type": "metaAndAssetCtxs", "dex": ""}, deadline=deadline
        )
        result = parse_open_interest(
            payload, snapshot_id=self.snapshot_id(), fetch_started_at=started, received_at=received
        )
        if self.monotonic() >= deadline:
            raise HyperliquidError(HyperliquidErrorCode.DEADLINE_EXCEEDED)
        return result
