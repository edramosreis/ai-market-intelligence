"""Explicit native history, request windows and correction-aware manual job policy."""

from datetime import date, datetime
from uuid import UUID

from market_intelligence.macro.models import CATALOG, MacroProvider, MacroSeries, MonthlyWindow, utc
from market_intelligence.macro.storage_models import MacroFailureCode, MacroStorageErrorCode

HISTORY_START = {
    MacroProvider.BLS: date(1947, 1, 1),
    MacroProvider.FED: date(1954, 7, 1),
}


def refresh_start(provider: MacroProvider, today: date) -> date:
    # CPI seasonal revisions revisit the preceding five years, not just recent months.
    return (
        max(HISTORY_START[provider], date(today.year - 5, 1, 1))
        if provider == MacroProvider.BLS
        else HISTORY_START[provider]
    )


def job_windows(
    provider: MacroProvider, window: MonthlyWindow, observed_now: datetime
) -> tuple[MonthlyWindow, ...]:
    if not isinstance(provider, MacroProvider):
        raise ValueError("Expected a fixed macro provider")
    current = utc(observed_now).date().replace(day=1)
    months = (window.end.year - window.start.year) * 12 + window.end.month - window.start.month
    if window.start < HISTORY_START[provider] or window.end > current or months > 1200:
        raise ValueError("Macro jobs require native history and completed monthly bounds")
    if provider == MacroProvider.FED:
        # One download already contains the full release; do not download once per month.
        return (window,)
    chunks = []
    start = window.start
    while start < window.end:
        end = (
            window.end
            if window.end.year - start.year < 10
            else min(window.end, date(start.year + 10, 1, 1))
        )
        chunks.append(MonthlyWindow(start, end))
        start = end
    return tuple(chunks)


def expected_periods(
    provider: MacroProvider, window: MonthlyWindow
) -> set[tuple[MacroSeries, date]]:
    """Expected native month keys, including unavailable values; no release-time claim."""
    periods = set()
    month = window.start
    while month < window.end:
        for series, metadata in CATALOG.items():
            if metadata.provider == provider.value and month >= metadata.earliest_month:
                periods.add((series, month))
        month = (
            date(month.year + 1, 1, 1)
            if month.month == 12
            else date(month.year, month.month + 1, 1)
        )
    return periods


def resume_eligible(provider: MacroProvider, window: MonthlyWindow, observed_now: datetime) -> bool:
    today = utc(observed_now).date()
    if provider == MacroProvider.BLS:
        return window.end <= refresh_start(provider, today)
    # A command including the latest completed month always reads the release again.
    return window.end < today.replace(day=1)


class MacroIngestionError(Exception):
    def __init__(
        self,
        code: MacroFailureCode,
        provider: MacroProvider,
        window: MonthlyWindow | None = None,
        run_id: UUID | None = None,
        *,
        storage_code: MacroStorageErrorCode | None = None,
    ) -> None:
        self.code, self.provider, self.window, self.run_id = code, provider, window, run_id
        self.storage_code = storage_code
        self.audit_recorded = False
        self.requests_used = 0
        self.request_limit: int | None = None
        super().__init__(code.value)
