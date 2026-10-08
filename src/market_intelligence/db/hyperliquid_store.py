"""Current funding corrections and immutable OI receipts with atomic success audits."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.db.tables import funding_events as facts
from market_intelligence.db.tables import funding_ingestion_runs as runs
from market_intelligence.db.tables import open_interest_runs as oi_runs
from market_intelligence.db.tables import open_interest_snapshots as snapshots
from market_intelligence.hyperliquid.models import (
    INSTRUMENT_CODE,
    SOURCE_CODE,
    FundingEvent,
    FundingReport,
    FundingWindow,
    HyperliquidErrorCode,
    HyperliquidIngestionError,
    OpenInterestSnapshot,
    expected_hours,
    utc,
)

FUNDING_LOCK_KEY = 202401
OI_LOCK_KEY = 202402


class HyperliquidStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def exclusive_writer(self, operation: str) -> Iterator[None]:
        if operation not in {"funding", "open_interest"}:
            raise ValueError("Unsupported Hyperliquid operation")
        key = FUNDING_LOCK_KEY if operation == "funding" else OI_LOCK_KEY
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            if not conn.execute(
                sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            ).scalar_one():
                raise HyperliquidIngestionError(HyperliquidErrorCode.CONCURRENT_JOB, operation)
            try:
                yield
            finally:
                try:
                    conn.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                except SQLAlchemyError:
                    conn.invalidate()

    def completed(self, window: FundingWindow) -> FundingReport | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(runs)
                    .where(
                        runs.c.source_code == SOURCE_CODE,
                        runs.c.instrument_code == INSTRUMENT_CODE,
                        runs.c.requested_start == window.start,
                        runs.c.requested_end == window.end,
                    )
                    .order_by(runs.c.started_at.desc(), runs.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if (
                row is None
                or row["status"] != "succeeded"
                or row["received"] != row["expected_hours"]
            ):
                return None
            stored = conn.execute(
                sa.select(sa.func.count())
                .select_from(facts)
                .where(
                    facts.c.source_code == SOURCE_CODE,
                    facts.c.instrument_code == INSTRUMENT_CODE,
                    facts.c.event_at >= window.start,
                    facts.c.event_at < window.end,
                )
            ).scalar_one()
            if stored != expected_hours(window):
                return None
            return FundingReport(
                row["id"],
                window,
                row["expected_hours"],
                row["received"],
                row["inserted"],
                row["updated"],
                row["unchanged"],
                row["retained"],
                True,
            )

    def start_funding(self, window: FundingWindow, started_at: datetime) -> UUID:
        run_id = uuid4()
        with self.engine.begin() as conn:
            conn.execute(
                runs.insert().values(
                    id=run_id,
                    source_code=SOURCE_CODE,
                    instrument_code=INSTRUMENT_CODE,
                    requested_start=window.start,
                    requested_end=window.end,
                    expected_hours=expected_hours(window),
                    started_at=utc(started_at),
                    status="running",
                )
            )
        return run_id

    def persist_funding(
        self,
        run_id: UUID,
        window: FundingWindow,
        events: Sequence[FundingEvent],
        finished_at: datetime,
    ) -> FundingReport:
        slots = {event.settlement_hour for event in events}
        if len(slots) != len(events) or any(
            not window.start <= event.event_at < window.end for event in events
        ):
            raise HyperliquidIngestionError(HyperliquidErrorCode.INVALID_PAYLOAD, "funding")
        finished_at = utc(finished_at)
        with self.engine.begin() as conn:
            run = (
                conn.execute(
                    sa.select(runs)
                    .where(
                        runs.c.id == run_id,
                        runs.c.status == "running",
                        runs.c.source_code == SOURCE_CODE,
                        runs.c.instrument_code == INSTRUMENT_CODE,
                        runs.c.requested_start == window.start,
                        runs.c.requested_end == window.end,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if run is None or finished_at < run["started_at"]:
                raise HyperliquidIngestionError(HyperliquidErrorCode.DATABASE_ERROR, "funding")
            existing = {
                row["settlement_hour"]: row
                for row in conn.execute(
                    sa.select(facts).where(
                        facts.c.source_code == SOURCE_CODE,
                        facts.c.instrument_code == INSTRUMENT_CODE,
                        facts.c.event_at >= window.start,
                        facts.c.event_at < window.end,
                    )
                ).mappings()
            }
            inserted = updated = unchanged = 0
            changes = []
            for event in events:
                previous = existing.get(event.settlement_hour)
                if previous is not None and previous["event_at"] != event.event_at:
                    # A shifted event identity needs inspection before changing its stored key.
                    raise HyperliquidIngestionError(HyperliquidErrorCode.INVALID_PAYLOAD, "funding")
                if previous is not None and (previous["funding_rate"], previous["premium"]) == (
                    event.funding_rate,
                    event.premium,
                ):
                    unchanged += 1
                    continue
                inserted += previous is None
                updated += previous is not None
                changes.append(
                    dict(
                        source_code=SOURCE_CODE,
                        instrument_code=INSTRUMENT_CODE,
                        event_at=event.event_at,
                        settlement_hour=event.settlement_hour,
                        funding_rate=event.funding_rate,
                        premium=event.premium,
                        first_ingested_at=finished_at,
                        last_updated_at=finished_at,
                        last_ingestion_run_id=run_id,
                    )
                )
            if changes:
                statement = insert(facts).values(changes)
                excluded = statement.excluded
                conn.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            facts.c.source_code,
                            facts.c.instrument_code,
                            facts.c.event_at,
                        ],
                        set_={
                            "funding_rate": excluded.funding_rate,
                            "premium": excluded.premium,
                            "last_updated_at": sa.func.greatest(
                                excluded.last_updated_at, facts.c.last_updated_at
                            ),
                            "last_ingestion_run_id": excluded.last_ingestion_run_id,
                        },
                        where=sa.or_(
                            facts.c.funding_rate.is_distinct_from(excluded.funding_rate),
                            facts.c.premium.is_distinct_from(excluded.premium),
                        ),
                    )
                )
            retained = len(existing.keys() - slots)
            report = FundingReport(
                run_id,
                window,
                expected_hours(window),
                len(events),
                inserted,
                updated,
                unchanged,
                retained,
            )
            conn.execute(
                runs.update()
                .where(runs.c.id == run_id)
                .values(
                    status="succeeded",
                    finished_at=finished_at,
                    received=len(events),
                    inserted=inserted,
                    updated=updated,
                    unchanged=unchanged,
                    retained=retained,
                )
            )
        return report

    def start_open_interest(self, started_at: datetime) -> UUID:
        run_id = uuid4()
        with self.engine.begin() as conn:
            conn.execute(
                oi_runs.insert().values(
                    id=run_id,
                    source_code=SOURCE_CODE,
                    instrument_code=INSTRUMENT_CODE,
                    started_at=utc(started_at),
                    status="running",
                )
            )
        return run_id

    def persist_open_interest(
        self, run_id: UUID, snapshot: OpenInterestSnapshot, finished_at: datetime
    ) -> None:
        finished_at = utc(finished_at)
        with self.engine.begin() as conn:
            run = (
                conn.execute(
                    sa.select(oi_runs)
                    .where(
                        oi_runs.c.id == run_id,
                        oi_runs.c.status == "running",
                        oi_runs.c.source_code == SOURCE_CODE,
                        oi_runs.c.instrument_code == INSTRUMENT_CODE,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                run is None
                or snapshot.fetch_started_at < run["started_at"]
                or snapshot.received_at > finished_at
            ):
                raise HyperliquidIngestionError(
                    HyperliquidErrorCode.INVALID_PAYLOAD, "open_interest"
                )
            conn.execute(
                snapshots.insert().values(
                    snapshot_id=snapshot.snapshot_id,
                    source_code=SOURCE_CODE,
                    instrument_code=INSTRUMENT_CODE,
                    fetch_started_at=snapshot.fetch_started_at,
                    received_at=snapshot.received_at,
                    open_interest_btc=snapshot.open_interest_btc,
                    mark_price_usdt=snapshot.mark_price_usdt,
                    oracle_price_usdt=snapshot.oracle_price_usdt,
                    ingestion_run_id=run_id,
                )
            )
            conn.execute(
                oi_runs.update()
                .where(oi_runs.c.id == run_id)
                .values(
                    status="succeeded",
                    finished_at=finished_at,
                    received=1,
                )
            )

    def fail_run(
        self, run_id: UUID, finished_at: datetime, code: HyperliquidErrorCode, operation: str
    ) -> None:
        table = runs if operation == "funding" else oi_runs
        with self.engine.begin() as conn:
            result = conn.execute(
                table.update()
                .where(table.c.id == run_id, table.c.status == "running")
                .values(
                    status="failed",
                    finished_at=utc(finished_at),
                    error_code=code.value,
                )
            )
            if result.rowcount != 1:
                raise HyperliquidIngestionError(HyperliquidErrorCode.DATABASE_ERROR, operation)
