"""Manual fetch/validate/commit orchestration, with no SQL transaction during HTTP."""

import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from market_intelligence.db.macro_store import MacroStore
from market_intelligence.macro.jobs import MacroIngestionError, job_windows, resume_eligible
from market_intelligence.macro.models import (
    MacroError,
    MacroErrorCode,
    MacroProvider,
    MonthlyWindow,
    ProviderRead,
    utc,
)
from market_intelligence.macro.storage_models import (
    MacroFailureCode,
    MacroStorageError,
    MacroStorageErrorCode,
    MacroWriteReport,
)


class MacroSource(Protocol):
    def fetch(self, window: MonthlyWindow, *, deadline: float) -> ProviderRead: ...


def ingest_macro(
    source: MacroSource,
    store: MacroStore,
    provider: MacroProvider,
    window: MonthlyWindow,
    *,
    resume: bool = False,
    window_seconds: float = 180,
    max_seconds: float = 900,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    progress: Callable[[MacroWriteReport], None] | None = None,
) -> list[MacroWriteReport]:
    observed_now = utc(now())
    windows = job_windows(provider, window, observed_now)
    if any(not math.isfinite(seconds) or seconds <= 0 for seconds in (window_seconds, max_seconds)):
        raise ValueError("Macro job budgets must be finite positive seconds")
    command_deadline = monotonic() + max_seconds
    reports = []
    for chunk in windows:
        identity = None
        try:
            if monotonic() >= command_deadline:
                raise MacroError(MacroErrorCode.DEADLINE_EXCEEDED)
            report = (
                store.completed(provider, chunk)
                if resume and resume_eligible(provider, chunk, observed_now)
                else None
            )
            if report is None:
                started = utc(now())
                identity = store.start_run(provider, chunk, started)
                deadline = min(command_deadline, monotonic() + window_seconds)
                read = source.fetch(chunk, deadline=deadline)
                if monotonic() >= deadline:
                    raise MacroError(MacroErrorCode.DEADLINE_EXCEEDED)
                report = store.persist(
                    identity, provider, read, max(utc(now()), started, read.received_at)
                )
        except (Exception, KeyboardInterrupt) as failure:
            storage_code = failure.code if isinstance(failure, MacroStorageError) else None
            if isinstance(failure, MacroError):
                code = MacroFailureCode(failure.code.value)
            elif isinstance(failure, MacroStorageError):
                code = {
                    MacroStorageErrorCode.INVALID_READ: MacroFailureCode.INVALID_PAYLOAD,
                    MacroStorageErrorCode.STALE_READ: MacroFailureCode.STALE_READ,
                }.get(failure.code, MacroFailureCode.DATABASE_ERROR)
            elif isinstance(failure, KeyboardInterrupt):
                code = MacroFailureCode.INTERRUPTED
            else:
                code = MacroFailureCode.INTERNAL_ERROR
            error = MacroIngestionError(code, provider, chunk, identity, storage_code=storage_code)
            if identity is not None:
                try:
                    store.fail_run(identity, code, max(utc(now()), started))
                    error.audit_recorded = True
                except MacroStorageError, ValueError:
                    pass
            raise error from None
        reports.append(report)
        if progress is not None:
            progress(report)
    return reports
