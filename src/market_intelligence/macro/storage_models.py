"""Bounded persistence arguments and reports; no provider release/vintage inference."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from market_intelligence.macro.bls import request_years
from market_intelligence.macro.models import (
    CATALOG,
    MacroProvider,
    MacroSeries,
    MonthlyWindow,
    ProviderRead,
)


class MacroStorageErrorCode(StrEnum):
    INVALID_READ = "invalid_read"
    INVALID_RUN = "invalid_run"
    STALE_READ = "stale_read"
    CONCURRENT_WRITE = "concurrent_write"
    DATABASE_ERROR = "database_error"


class MacroStorageError(Exception):
    def __init__(self, code: MacroStorageErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class MacroFailureCode(StrEnum):
    INVALID_PAYLOAD = "invalid_payload"
    SOURCE_REJECTED = "source_rejected"
    HTTP_ERROR = "http_error"
    RETRY_EXHAUSTED = "retry_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    DATABASE_ERROR = "database_error"
    INTERRUPTED = "interrupted"
    INTERNAL_ERROR = "internal_error"
    STALE_READ = "stale_read"


def validate_window(provider: MacroProvider, window: MonthlyWindow) -> int:
    if not isinstance(provider, MacroProvider) or not isinstance(window, MonthlyWindow):
        raise ValueError("Expected a fixed provider and monthly window")
    months = (window.end.year - window.start.year) * 12 + window.end.month - window.start.month
    if window.start < date(1947, 1, 1) or months > 1200:
        raise ValueError("Macro persistence windows are bounded to one hundred years from 1947")
    if provider == MacroProvider.BLS:
        request_years(window)
    return months * (2 if provider == MacroProvider.BLS else 1)


def validate_read(provider: MacroProvider, read: ProviderRead) -> None:
    try:
        capacity = validate_window(provider, read.window)
        if (
            not isinstance(read.observations, tuple)
            or len(read.observations) > capacity
            or any(CATALOG[row.series].provider != provider.value for row in read.observations)
            or type(read.annual_average_count) is not int
            or not 0 <= read.annual_average_count <= 20
            or not isinstance(read.source_messages, tuple)
            or len(read.source_messages) > 50
            or any(not isinstance(msg, str) or len(msg) > 4000 for msg in read.source_messages)
            or not isinstance(read.latest_hints, tuple)
            or len(read.latest_hints) > 2
            or not isinstance(read.source_annotations, tuple)
            or len(read.source_annotations) > 20
        ):
            raise ValueError("Invalid receipt metadata")
        hinted: set[MacroSeries] = set()
        for series, month in read.latest_hints:
            if (
                not isinstance(series, MacroSeries)
                or series in hinted
                or CATALOG[series].provider != provider.value
                or type(month) is not date
                or month.day != 1
                or not read.window.start <= month < read.window.end
                or (series, month) not in {(row.series, row.month) for row in read.observations}
            ):
                raise ValueError("Invalid latest hint")
            hinted.add(series)
        labels: set[str] = set()
        for label, text in read.source_annotations:
            if (
                not isinstance(label, str)
                or not 1 <= len(label) <= 100
                or label in labels
                or not isinstance(text, str)
                or not 1 <= len(text) <= 4000
            ):
                raise ValueError("Invalid source annotations")
            labels.add(label)
        if provider == MacroProvider.BLS:
            if read.prepared_text is not None or read.source_annotations:
                raise ValueError("Unexpected BLS release metadata")
        else:
            if read.annual_average_count or read.source_messages or read.latest_hints:
                raise ValueError("Unexpected Fed response metadata")
            if not isinstance(read.prepared_text, str) or not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}",
                read.prepared_text,
            ):
                raise ValueError("Invalid prepared text")
            datetime.fromisoformat(read.prepared_text)
            if any(row.footnotes for row in read.observations):
                raise ValueError("Unverified Fed observation footnotes")
    except ValueError, TypeError, KeyError:
        raise MacroStorageError(MacroStorageErrorCode.INVALID_READ) from None


@dataclass(frozen=True)
class MacroWriteReport:
    run_id: UUID
    provider: MacroProvider
    window: MonthlyWindow
    received: int
    inserted: int
    corrected: int
    unchanged: int
    source_missing: int
    retained_periods: tuple[tuple[MacroSeries, date], ...]
    annual_average_count: int

    @property
    def retained(self) -> int:
        return len(self.retained_periods)

    @property
    def versions_written(self) -> int:
        return self.inserted + self.corrected
