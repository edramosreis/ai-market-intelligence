"""Treasury source dates, supported maturities, exact yields, and missing values."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class TreasuryErrorCode(StrEnum):
    INVALID_PAYLOAD = "invalid_payload"
    HTTP_ERROR = "http_error"
    RETRY_EXHAUSTED = "retry_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"


class TreasuryError(Exception):
    def __init__(self, code: TreasuryErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class TreasuryTenor(StrEnum):
    ONE_MONTH = "1M"
    ONE_AND_HALF_MONTHS = "1.5M"
    TWO_MONTHS = "2M"
    THREE_MONTHS = "3M"
    FOUR_MONTHS = "4M"
    SIX_MONTHS = "6M"
    ONE_YEAR = "1Y"
    TWO_YEARS = "2Y"
    THREE_YEARS = "3Y"
    FIVE_YEARS = "5Y"
    SEVEN_YEARS = "7Y"
    TEN_YEARS = "10Y"
    TWENTY_YEARS = "20Y"
    THIRTY_YEARS = "30Y"


class TreasuryMissingReason(StrEnum):
    SOURCE_NULL = "source_null"
    FIELD_ABSENT = "field_absent"


@dataclass(frozen=True)
class TreasuryMonth:
    year: int
    month: int

    def __post_init__(self) -> None:
        if type(self.year) is not int or not 1990 <= self.year <= 9998:
            raise ValueError("Treasury year must be between 1990 and 9998")
        if type(self.month) is not int or not 1 <= self.month <= 12:
            raise ValueError("Invalid Treasury month")

    @property
    def start(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def end(self) -> date:
        return date(self.year + 1, 1, 1) if self.month == 12 else date(self.year, self.month + 1, 1)

    @property
    def provider_month(self) -> str:
        return f"{self.year:04d}{self.month:02d}"


@dataclass(frozen=True)
class TreasuryRate:
    tenor: TreasuryTenor
    yield_percent: Decimal | None
    missing_reason: TreasuryMissingReason | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tenor, TreasuryTenor):
            raise ValueError("Unsupported Treasury tenor")
        value = self.yield_percent
        if value is None:
            if not isinstance(self.missing_reason, TreasuryMissingReason):
                raise ValueError("Missing yield requires a reason")
            return
        if (
            self.missing_reason is not None
            or not isinstance(value, Decimal)
            or not value.is_finite()
        ):
            raise ValueError("Invalid Treasury yield")
        exponent = value.as_tuple().exponent
        # Nominal par yields are floored at zero by Treasury. Real yields would
        # need a separate contract; exact values fit the existing numeric(38,18) policy.
        if value < 0 or not isinstance(exponent, int) or exponent < -18 or value.adjusted() >= 20:
            raise ValueError("Treasury yield exceeds nominal rate/precision bounds")


@dataclass(frozen=True)
class TreasuryCurve:
    observed_on: date
    rates: tuple[TreasuryRate, ...]

    def __post_init__(self) -> None:
        if type(self.observed_on) is not date:
            raise ValueError("Treasury observations use a date, not an event timestamp")
        if not isinstance(self.rates, tuple) or any(
            not isinstance(rate, TreasuryRate) for rate in self.rates
        ):
            raise ValueError("Invalid Treasury curve")
        if tuple(rate.tenor for rate in self.rates) != tuple(TreasuryTenor):
            raise ValueError("Curve must describe each supported tenor once, in maturity order")
