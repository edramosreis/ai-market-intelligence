"""Bounded monthly XML reads with strict normalization and injectable retries."""

import math
import random
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from market_intelligence.treasury.models import (
    TreasuryCurve,
    TreasuryError,
    TreasuryErrorCode,
    TreasuryMissingReason,
    TreasuryMonth,
    TreasuryRate,
    TreasuryTenor,
)

FEED_URL = "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml"
MAX_RESPONSE_BYTES = 2_000_000
ATOM = "{http://www.w3.org/2005/Atom}"
METADATA = "{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}"
DATA = "{http://schemas.microsoft.com/ado/2007/08/dataservices}"
FIELDS = dict(
    zip(
        (
            "BC_1MONTH",
            "BC_1_5MONTH",
            "BC_2MONTH",
            "BC_3MONTH",
            "BC_4MONTH",
            "BC_6MONTH",
            "BC_1YEAR",
            "BC_2YEAR",
            "BC_3YEAR",
            "BC_5YEAR",
            "BC_7YEAR",
            "BC_10YEAR",
            "BC_20YEAR",
            "BC_30YEAR",
        ),
        TreasuryTenor,
        strict=True,
    )
)
KNOWN_METADATA = {"NEW_DATE", "Id", "BC_30YEARDISPLAY"}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _rate(
    field: str, tenor: TreasuryTenor, properties: dict[str, ElementTree.Element]
) -> TreasuryRate:
    element = properties.get(field)
    if element is None:
        return TreasuryRate(tenor, None, TreasuryMissingReason.FIELD_ABSENT)
    if len(element):
        raise ValueError("Nested rate field")
    null = element.get(METADATA + "null")
    text = (element.text or "").strip()
    if null == "true":
        if text:
            raise ValueError("Null field also has a value")
        return TreasuryRate(tenor, None, TreasuryMissingReason.SOURCE_NULL)
    if null not in (None, "false") or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        raise ValueError("Malformed rate field")
    return TreasuryRate(tenor, Decimal(text))


def parse_treasury_xml(payload: bytes, month: TreasuryMonth) -> list[TreasuryCurve]:
    """Return source dates only; no calendar gaps, interpolation, or UTC timestamps."""
    try:
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("Oversized XML")
        text = payload.decode("utf-8-sig")
        # Decode UTF-8 first so an encoded DTD cannot bypass the declaration check.
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError("XML document types/entities are unsupported")
        root = ElementTree.fromstring(text)
        if root.tag != ATOM + "feed" or any(
            link.get("rel") == "next" for link in root.findall(ATOM + "link")
        ):
            raise ValueError("Unexpected or paginated monthly feed")
        entries = root.findall(ATOM + "entry")
        if len(entries) > 31:
            raise ValueError("Too many daily observations for one month")
        result: dict[date, TreasuryCurve] = {}
        for entry in entries:
            containers = entry.findall(ATOM + "content/" + METADATA + "properties")
            if len(containers) != 1:
                raise ValueError("Entry must have one properties container")
            properties: dict[str, ElementTree.Element] = {}
            for element in containers[0]:
                if not element.tag.startswith(DATA):
                    raise ValueError("Unexpected property namespace")
                name = element.tag[len(DATA) :]
                if name in properties or name not in FIELDS.keys() | KNOWN_METADATA:
                    raise ValueError("Duplicate or unsupported Treasury property")
                properties[name] = element
            observed = properties.get("NEW_DATE")
            if (
                observed is None
                or len(observed)
                or observed.get(METADATA + "null") not in (None, "false")
            ):
                raise ValueError("Missing observation date")
            raw_date = (observed.text or "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T00:00:00", raw_date):
                raise ValueError("Unexpected source date format")
            observed_on = date.fromisoformat(raw_date[:10])
            if not month.start <= observed_on < month.end or observed_on in result:
                raise ValueError("Out-of-month or duplicate source date")
            if not properties.keys() & FIELDS.keys():
                raise ValueError("Entry has no supported nominal curve fields")
            rates = tuple(_rate(field, tenor, properties) for field, tenor in FIELDS.items())
            # BC_30YEAR is canonical. The legacy display field is not a second
            # maturity and never substitutes for a missing primary rate.
            result[observed_on] = TreasuryCurve(observed_on, rates)
        return sorted(result.values(), key=lambda curve: curve.observed_on)
    except ValueError, UnicodeError, ElementTree.ParseError, InvalidOperation:
        raise TreasuryError(TreasuryErrorCode.INVALID_PAYLOAD) from None


class TreasuryClient:
    def __init__(
        self,
        client: httpx.Client,
        *,
        attempts: int = 3,
        request_interval: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = _utc_now,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise ValueError("Treasury attempts must be between one and five")
        if not math.isfinite(request_interval) or request_interval < 0:
            raise ValueError("Invalid Treasury request interval")
        self.client, self.attempts, self.request_interval = client, attempts, request_interval
        self.monotonic, self.sleep, self.now, self.jitter = monotonic, sleep, now, jitter
        self.last_request: float | None = None

    def _pause(self, seconds: float, deadline: float) -> None:
        if not math.isfinite(seconds) or seconds < 0 or self.monotonic() + seconds >= deadline:
            raise TreasuryError(TreasuryErrorCode.DEADLINE_EXCEEDED)
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
            return max(0, seconds) if math.isfinite(seconds) and seconds >= 0 else None
        except ValueError, TypeError, OverflowError:
            return None

    def fetch_month(self, month: TreasuryMonth, *, deadline: float) -> list[TreasuryCurve]:
        if not math.isfinite(deadline):
            raise ValueError("Treasury deadline must be finite")
        for attempt in range(self.attempts):
            if self.last_request is not None:
                self._pause(
                    max(0, self.request_interval - (self.monotonic() - self.last_request)), deadline
                )
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise TreasuryError(TreasuryErrorCode.DEADLINE_EXCEEDED)
            self.last_request = self.monotonic()
            retry_after: str | None = None
            try:
                with self.client.stream(
                    "GET",
                    FEED_URL,
                    params={
                        "data": "daily_treasury_yield_curve",
                        "field_tdr_date_value_month": month.provider_month,
                    },
                    follow_redirects=False,
                    timeout=httpx.Timeout(min(20, remaining), connect=min(5, remaining)),
                ) as response:
                    if self.monotonic() >= deadline:
                        raise TreasuryError(TreasuryErrorCode.DEADLINE_EXCEEDED)
                    if response.status_code not in {429, 500, 502, 503, 504}:
                        if response.status_code != 200:
                            raise TreasuryError(TreasuryErrorCode.HTTP_ERROR)
                        body = bytearray()
                        for chunk in response.iter_bytes(chunk_size=65536):
                            if self.monotonic() >= deadline:
                                raise TreasuryError(TreasuryErrorCode.DEADLINE_EXCEEDED)
                            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise TreasuryError(TreasuryErrorCode.INVALID_PAYLOAD)
                            body.extend(chunk)
                        curves = parse_treasury_xml(bytes(body), month)
                        if self.monotonic() >= deadline:
                            raise TreasuryError(TreasuryErrorCode.DEADLINE_EXCEEDED)
                        return curves
                    retry_after = response.headers.get("retry-after")
            except httpx.TransportError:
                if self.monotonic() >= deadline:
                    raise TreasuryError(TreasuryErrorCode.DEADLINE_EXCEEDED) from None
            if attempt + 1 == self.attempts:
                raise TreasuryError(TreasuryErrorCode.RETRY_EXHAUSTED)
            wait = self._retry_after(retry_after)
            self._pause(wait if wait is not None else min(30, 2**attempt + self.jitter()), deadline)
        raise AssertionError("Retry loop must return or raise")
