"""Monthly fetch/validate/commit orchestration and explicit resume semantics."""

import math
import time
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.db.candle_store import CandleStore
from market_intelligence.ingestion.coinbase import CoinbaseClient
from market_intelligence.ingestion.models import (
    ChunkReport,
    ErrorCode,
    IngestionError,
    TimeWindow,
    closed_cutoff,
    monthly_windows,
    utc_now,
)


def ingest(
    source: CoinbaseClient,
    store: CandleStore,
    window: TimeWindow,
    *,
    resume: bool = False,
    chunk_seconds: float = 600,
    max_seconds: float | None = None,
    now: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    progress: Callable[[ChunkReport], None] | None = None,
) -> list[ChunkReport]:
    if (
        not math.isfinite(chunk_seconds)
        or chunk_seconds <= 0
        or (max_seconds is not None and (not math.isfinite(max_seconds) or max_seconds <= 0))
    ):
        raise ValueError("Deadlines must be finite positive seconds")
    cutoff = closed_cutoff(now())
    if window.end > cutoff:
        raise ValueError("Requested end exceeds the closed-candle cutoff")
    command_deadline = monotonic() + max_seconds if max_seconds is not None else math.inf
    reports = []
    verified = False
    with store.exclusive_writer():
        market_id = store.market_id()
        for chunk in monthly_windows(window):
            if monotonic() >= command_deadline:
                error = IngestionError(ErrorCode.DEADLINE_EXCEEDED)
                error.window = chunk
                raise error
            report = store.completed(market_id, chunk) if resume else None
            if report is None:
                started = now()
                run_id = store.start_run(market_id, chunk, started)
                try:
                    deadline = min(command_deadline, monotonic() + chunk_seconds)
                    if not verified:
                        source.verify_product(deadline)
                        verified = True
                    observations = source.fetch_chunk(chunk, cutoff, deadline)
                    if monotonic() >= deadline:
                        raise IngestionError(ErrorCode.DEADLINE_EXCEEDED)
                    report = store.persist(
                        run_id, market_id, chunk, observations, max(now(), started)
                    )
                except (Exception, KeyboardInterrupt) as failure:
                    if isinstance(failure, IngestionError):
                        code = failure.code
                    elif isinstance(failure, SQLAlchemyError):
                        code = ErrorCode.DATABASE_ERROR
                    elif isinstance(failure, KeyboardInterrupt):
                        code = ErrorCode.INTERRUPTED
                    else:
                        code = ErrorCode.INTERNAL_ERROR
                    error = IngestionError(code)
                    error.window, error.run_id = chunk, run_id
                    try:
                        store.fail_run(run_id, max(now(), started), code)
                        error.audit_recorded = True
                    except SQLAlchemyError, IngestionError:
                        pass
                    raise error from None
            reports.append(report)
            if progress is not None:
                progress(report)
    return reports
