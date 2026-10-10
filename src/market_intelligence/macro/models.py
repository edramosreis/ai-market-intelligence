"""Native monthly identities, fixed source metadata and local receipt evidence."""

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum


class MacroErrorCode(StrEnum):
    INVALID_PAYLOAD = "invalid_payload"
    SOURCE_REJECTED = "source_rejected"
    HTTP_ERROR = "http_error"
    RETRY_EXHAUSTED = "retry_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"


class MacroError(Exception):
    def __init__(self, code: MacroErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class MacroSeries(StrEnum):
    CPI = "CUSR0000SA0"
    UNEMPLOYMENT = "LNS14000000"
    FED_FUNDS = "RIFSPFF_N.M"


class MacroProvider(StrEnum):
    BLS = "bls"
    FED = "federal_reserve_board"


@dataclass(frozen=True)
class SeriesMetadata:
    provider: str
    title: str
    unit: str
    seasonal_adjustment: str
    earliest_month: date


CATALOG = {
    MacroSeries.CPI: SeriesMetadata(
        "bls",
        "CPI-U all items, US city average",
        "index_1982_84_100",
        "seasonally_adjusted",
        date(1947, 1, 1),
    ),
    MacroSeries.UNEMPLOYMENT: SeriesMetadata(
        "bls",
        "Unemployment rate, civilian population age 16 and over",
        "percent",
        "seasonally_adjusted",
        date(1948, 1, 1),
    ),
    MacroSeries.FED_FUNDS: SeriesMetadata(
        "federal_reserve_board",
        "Monthly effective federal funds rate",
        "percent_per_annum",
        "not_seasonally_adjusted",
        date(1954, 7, 1),
    ),
}
BLS_NOTICE = (
    "BLS.gov cannot vouch for the data or analyses derived from these data "
    "after the data have been retrieved from BLS.gov."
)


def utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Expected an aware local timestamp")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class MonthlyWindow:
    """Half-open monthly bounds, with no inference about source availability."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if (
            type(self.start) is not date
            or type(self.end) is not date
            or self.start.day != 1
            or self.end.day != 1
            or self.start >= self.end
            or self.end.year > 9998
        ):
            raise ValueError("Expected ordered month-aligned dates")


@dataclass(frozen=True)
class Footnote:
    code: str
    text: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.code, str)
            or not 1 <= len(self.code) <= 32
            or not isinstance(self.text, str)
            or not 1 <= len(self.text) <= 4000
        ):
            raise ValueError("Invalid source footnote")


@dataclass(frozen=True)
class MonthlyObservation:
    series: MacroSeries
    month: date
    native_period: str
    value: Decimal | None
    missing_reason: str | None = None
    footnotes: tuple[Footnote, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.series, MacroSeries):
            raise ValueError("Unsupported macro series")
        if (
            type(self.month) is not date
            or self.month.day != 1
            or self.month < CATALOG[self.series].earliest_month
            or self.month.year > 9998
        ):
            raise ValueError("Invalid native observation month")
        native = (
            date(
                self.month.year,
                self.month.month,
                calendar.monthrange(self.month.year, self.month.month)[1],
            ).isoformat()
            if self.series == MacroSeries.FED_FUNDS
            else f"{self.month.year:04d}-M{self.month.month:02d}"
        )
        if self.native_period != native:
            raise ValueError("Native label and canonical month disagree")
        if (
            not isinstance(self.footnotes, tuple)
            or len(self.footnotes) > 20
            or any(not isinstance(note, Footnote) for note in self.footnotes)
        ):
            raise ValueError("Invalid footnotes")
        if self.value is None:
            if self.series == MacroSeries.FED_FUNDS or self.missing_reason != "source_dash":
                raise ValueError("Unsupported missing value semantics")
        elif (
            not isinstance(self.value, Decimal)
            or not self.value.is_finite()
            or not isinstance(self.value.as_tuple().exponent, int)
            or int(self.value.as_tuple().exponent) < -18
            or self.value.adjusted() >= 20
            or self.missing_reason is not None
            or (self.series == MacroSeries.CPI and self.value <= 0)
            or (self.series == MacroSeries.UNEMPLOYMENT and not 0 <= self.value <= 100)
            or (self.series == MacroSeries.FED_FUNDS and self.value == Decimal("-9999"))
        ):
            raise ValueError("Invalid native monthly value")


def decimal_text(value: object) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("Expected a native decimal string")
    return Decimal(value)


@dataclass(frozen=True)
class ProviderRead:
    """Successful validation, not publication-calendar completeness or a historical vintage."""

    window: MonthlyWindow
    observations: tuple[MonthlyObservation, ...]
    fetch_started_at: datetime
    received_at: datetime
    annual_average_count: int = 0
    source_messages: tuple[str, ...] = ()
    latest_hints: tuple[tuple[MacroSeries, date], ...] = ()
    prepared_text: str | None = None
    source_annotations: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fetch_started_at", utc(self.fetch_started_at))
        object.__setattr__(self, "received_at", utc(self.received_at))
        if self.received_at < self.fetch_started_at:
            raise ValueError("Receipt precedes fetch")
        keys = [(row.series.value, row.month) for row in self.observations]
        if keys != sorted(set(keys)) or any(
            not self.window.start <= row.month < self.window.end for row in self.observations
        ):
            raise ValueError("Duplicate, unordered or out-of-window observations")
