"""Explicit monthly Treasury ingestion; no scheduler or publication-calendar inference."""

import math
import time
from collections.abc import Callable
from datetime import date, datetime

from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.db.treasury_store import TreasuryStore
from market_intelligence.ingestion.models import as_utc, utc_now
from market_intelligence.treasury.client import TreasuryClient
from market_intelligence.treasury.models import (
    TreasuryError,
    TreasuryErrorCode,
    TreasuryIngestionError,
    TreasuryMonth,
    TreasuryReport,
    treasury_months,
)


def ingest_treasury(
    source: TreasuryClient,
    store: TreasuryStore,
    start: date,
    end: date,
    *,
    resume: bool = False,
    month_seconds: float = 60,
    max_seconds: float | None = None,
    now: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    progress: Callable[[TreasuryReport], None] | None = None,
) -> list[TreasuryReport]:
    months = treasury_months(start, end)
    if (
        not math.isfinite(month_seconds)
        or month_seconds <= 0
        or (max_seconds is not None and (not math.isfinite(max_seconds) or max_seconds <= 0))
    ):
        raise ValueError("Treasury budgets must be finite positive seconds")
    today = as_utc(now()).date()
    current = TreasuryMonth(today.year, today.month)
    if end > current.end:
        raise ValueError("Cannot ingest future Treasury months")
    command_deadline = monotonic() + max_seconds if max_seconds is not None else math.inf
    reports = []
    try:
        with store.exclusive_writer():
            for month in months:
                if monotonic() >= command_deadline:
                    error = TreasuryIngestionError(TreasuryErrorCode.DEADLINE_EXCEEDED)
                    error.month = month
                    raise error
                report = store.completed(month) if resume and month.start < current.start else None
                if report is None:
                    started = as_utc(now())
                    run_id = store.start_run(month, started)
                    try:
                        deadline = min(command_deadline, monotonic() + month_seconds)
                        curves = source.fetch_month(month, deadline=deadline)
                        if monotonic() >= deadline:
                            raise TreasuryError(TreasuryErrorCode.DEADLINE_EXCEEDED)
                        if any(curve.observed_on > as_utc(now()).date() for curve in curves):
                            raise TreasuryError(TreasuryErrorCode.INVALID_PAYLOAD)
                        report = store.persist(run_id, month, curves, max(as_utc(now()), started))
                    except (Exception, KeyboardInterrupt) as failure:
                        if isinstance(failure, TreasuryError):
                            code = failure.code
                        elif isinstance(failure, SQLAlchemyError):
                            code = TreasuryErrorCode.DATABASE_ERROR
                        elif isinstance(failure, KeyboardInterrupt):
                            code = TreasuryErrorCode.INTERRUPTED
                        else:
                            code = TreasuryErrorCode.INTERNAL_ERROR
                        error = TreasuryIngestionError(code)
                        error.month, error.run_id = month, run_id
                        try:
                            store.fail_run(run_id, max(as_utc(now()), started), code)
                            error.audit_recorded = True
                        except SQLAlchemyError, TreasuryError:
                            pass
                        raise error from None
                reports.append(report)
                if progress:
                    progress(report)
    except SQLAlchemyError:
        raise TreasuryIngestionError(TreasuryErrorCode.DATABASE_ERROR) from None
    return reports
