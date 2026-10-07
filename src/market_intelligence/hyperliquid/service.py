"""Manual monthly funding and one-shot OI collection with separate writer locks."""

import math
import time
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.db.hyperliquid_store import HyperliquidStore
from market_intelligence.hyperliquid.client import HyperliquidClient
from market_intelligence.hyperliquid.models import (
    FundingReport,
    FundingWindow,
    HyperliquidError,
    HyperliquidErrorCode,
    HyperliquidIngestionError,
    OpenInterestSnapshot,
    funding_windows,
    utc,
)
from market_intelligence.ingestion.models import utc_now


def _budget(seconds: float) -> None:
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Job deadlines must be positive and finite")


def _failure(
    failure: BaseException,
    store: HyperliquidStore,
    run_id: UUID,
    started_at: datetime,
    finished_at: datetime,
    operation: str,
    window: FundingWindow | None = None,
) -> HyperliquidIngestionError:
    if isinstance(failure, HyperliquidError):
        code = failure.code
    elif isinstance(failure, SQLAlchemyError):
        code = HyperliquidErrorCode.DATABASE_ERROR
    elif isinstance(failure, KeyboardInterrupt):
        code = HyperliquidErrorCode.INTERRUPTED
    else:
        code = HyperliquidErrorCode.INTERNAL_ERROR
    error = HyperliquidIngestionError(code, operation)
    error.window, error.run_id = window, run_id
    try:
        store.fail_run(run_id, max(utc(finished_at), started_at), code, operation)
        error.audit_recorded = True
    except SQLAlchemyError, HyperliquidError:
        pass
    return error


def ingest_funding(
    source: HyperliquidClient,
    store: HyperliquidStore,
    start: datetime,
    end: datetime,
    *,
    resume: bool = False,
    month_seconds: float = 90,
    max_seconds: float = 900,
    now: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    progress: Callable[[FundingReport], None] | None = None,
) -> list[FundingReport]:
    windows = funding_windows(start, end)
    if utc(end) > utc(now()):
        raise ValueError("Funding ingestion cannot request future events")
    _budget(month_seconds)
    _budget(max_seconds)
    deadline = monotonic() + max_seconds
    reports = []
    try:
        with store.exclusive_writer("funding"):
            for window in windows:
                if monotonic() >= deadline:
                    error = HyperliquidIngestionError(
                        HyperliquidErrorCode.DEADLINE_EXCEEDED, "funding"
                    )
                    error.window = window
                    raise error
                current = utc(now()).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                report = store.completed(window) if resume and window.end <= current else None
                if report is None:
                    started = utc(now())
                    run_id = store.start_funding(window, started)
                    try:
                        chunk_deadline = min(deadline, monotonic() + month_seconds)
                        events = source.fetch_funding(window, deadline=chunk_deadline)
                        if monotonic() >= chunk_deadline:
                            raise HyperliquidError(HyperliquidErrorCode.DEADLINE_EXCEEDED)
                        report = store.persist_funding(
                            run_id, window, events, max(utc(now()), started)
                        )
                    except (Exception, KeyboardInterrupt) as failure:
                        raise _failure(
                            failure, store, run_id, started, now(), "funding", window
                        ) from None
                reports.append(report)
                if progress:
                    progress(report)
    except SQLAlchemyError:
        raise HyperliquidIngestionError(HyperliquidErrorCode.DATABASE_ERROR, "funding") from None
    return reports


def collect_open_interest(
    source: HyperliquidClient,
    store: HyperliquidStore,
    *,
    seconds: float = 30,
    now: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[UUID, OpenInterestSnapshot]:
    _budget(seconds)
    deadline = monotonic() + seconds
    try:
        with store.exclusive_writer("open_interest"):
            started = utc(now())
            run_id = store.start_open_interest(started)
            try:
                snapshot = source.fetch_open_interest(deadline=deadline)
                if monotonic() >= deadline:
                    raise HyperliquidError(HyperliquidErrorCode.DEADLINE_EXCEEDED)
                store.persist_open_interest(run_id, snapshot, max(utc(now()), started))
            except (Exception, KeyboardInterrupt) as failure:
                raise _failure(failure, store, run_id, started, now(), "open_interest") from None
    except SQLAlchemyError:
        raise HyperliquidIngestionError(
            HyperliquidErrorCode.DATABASE_ERROR, "open_interest"
        ) from None
    return run_id, snapshot
