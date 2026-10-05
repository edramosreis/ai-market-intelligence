"""PostgreSQL chunk persistence; HTTP work stays outside write transactions."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.db.tables import candles, ingestion_runs, markets
from market_intelligence.ingestion.models import (
    INTERVAL_SECONDS,
    Candle,
    ChunkReport,
    ErrorCode,
    IngestionError,
    TimeWindow,
)

INGESTION_LOCK_KEY = 3002020
VALUE_FIELDS = ("open", "high", "low", "close", "base_volume")


class CandleStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def exclusive_writer(self) -> Iterator[None]:
        # Session-scoped lock with autocommit: no idle transaction during network waits.
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            locked = conn.execute(
                sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": INGESTION_LOCK_KEY}
            ).scalar_one()
            if not locked:
                raise IngestionError(ErrorCode.CONCURRENT_JOB)
            try:
                yield
            finally:
                try:
                    conn.execute(
                        sa.text("SELECT pg_advisory_unlock(:key)"), {"key": INGESTION_LOCK_KEY}
                    )
                except SQLAlchemyError:
                    # A pooled session must not retain a lock if its unlock failed.
                    conn.invalidate()

    def market_id(self) -> int:
        with self.engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    markets.c.id, markets.c.base_asset_code, markets.c.quote_asset_code
                ).where(
                    markets.c.source_code == "coinbase_exchange",
                    markets.c.source_product_id == "BTC-USD",
                )
            ).one()
        if (row.base_asset_code, row.quote_asset_code) != ("BTC", "USD"):
            raise IngestionError(ErrorCode.PRODUCT_MISMATCH)
        return int(row.id)

    def completed(self, market_id: int, window: TimeWindow) -> ChunkReport | None:
        with self.engine.connect() as conn:
            run_id = conn.execute(
                sa.select(ingestion_runs.c.id)
                .where(
                    ingestion_runs.c.market_id == market_id,
                    ingestion_runs.c.interval_seconds == INTERVAL_SECONDS,
                    ingestion_runs.c.requested_start == window.start,
                    ingestion_runs.c.requested_end == window.end,
                    ingestion_runs.c.status == "succeeded",
                )
                .order_by(ingestion_runs.c.finished_at.desc(), ingestion_runs.c.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if run_id is None:
                return None
            # Current database coverage, rather than assuming the original run filled every bucket.
            count = conn.execute(
                sa.select(sa.func.count())
                .select_from(candles)
                .where(
                    candles.c.market_id == market_id,
                    candles.c.interval_seconds == INTERVAL_SECONDS,
                    candles.c.opened_at >= window.start,
                    candles.c.opened_at < window.end,
                )
            ).scalar_one()
            return ChunkReport(run_id, window, count, 0, 0, 0, window.expected - count, True)

    def start_run(self, market_id: int, window: TimeWindow, started_at: datetime) -> UUID:
        run_id = uuid4()
        with self.engine.begin() as conn:
            conn.execute(
                ingestion_runs.insert().values(
                    id=run_id,
                    market_id=market_id,
                    interval_seconds=INTERVAL_SECONDS,
                    requested_start=window.start,
                    requested_end=window.end,
                    started_at=started_at,
                    status="running",
                )
            )
        return run_id

    def persist(
        self,
        run_id: UUID,
        market_id: int,
        window: TimeWindow,
        observations: Sequence[Candle],
        finished_at: datetime,
    ) -> ChunkReport:
        with self.engine.begin() as conn:
            existing = {
                row["opened_at"]: tuple(row[name] for name in VALUE_FIELDS)
                for row in conn.execute(
                    sa.select(candles).where(
                        candles.c.market_id == market_id,
                        candles.c.interval_seconds == INTERVAL_SECONDS,
                        candles.c.opened_at >= window.start,
                        candles.c.opened_at < window.end,
                    )
                ).mappings()
            }
            inserted = updated = unchanged = 0
            changes = []
            for candle in observations:
                values = tuple(getattr(candle, name) for name in VALUE_FIELDS)
                previous = existing.get(candle.opened_at)
                if previous == values:
                    unchanged += 1
                    continue
                if previous is None:
                    inserted += 1
                else:
                    updated += 1
                changes.append(
                    {
                        "market_id": market_id,
                        "interval_seconds": INTERVAL_SECONDS,
                        "opened_at": candle.opened_at,
                        **dict(zip(VALUE_FIELDS, values, strict=True)),
                        "first_ingested_at": finished_at,
                        "last_updated_at": finished_at,
                        "last_ingestion_run_id": run_id,
                    }
                )
            for offset in range(0, len(changes), 500):
                statement = insert(candles).values(changes[offset : offset + 500])
                excluded = statement.excluded
                conn.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            candles.c.market_id,
                            candles.c.interval_seconds,
                            candles.c.opened_at,
                        ],
                        set_={
                            **{name: excluded[name] for name in VALUE_FIELDS},
                            "last_updated_at": sa.func.greatest(
                                excluded.last_updated_at, candles.c.last_updated_at
                            ),
                            "last_ingestion_run_id": excluded.last_ingestion_run_id,
                        },
                        where=sa.or_(
                            *(
                                candles.c[name].is_distinct_from(excluded[name])
                                for name in VALUE_FIELDS
                            )
                        ),
                    )
                )
            report = ChunkReport(
                run_id,
                window,
                len(observations),
                inserted,
                updated,
                unchanged,
                window.expected - len(observations),
            )
            result = conn.execute(
                ingestion_runs.update()
                .where(ingestion_runs.c.id == run_id, ingestion_runs.c.status == "running")
                .values(
                    status="succeeded",
                    finished_at=finished_at,
                    received=report.received,
                    inserted=inserted,
                    updated=updated,
                    unchanged=unchanged,
                    missing_buckets=report.missing_buckets,
                )
            )
            if result.rowcount != 1:
                raise IngestionError(ErrorCode.DATABASE_ERROR)
        return report

    def fail_run(self, run_id: UUID, finished_at: datetime, code: ErrorCode) -> None:
        with self.engine.begin() as conn:
            result = conn.execute(
                ingestion_runs.update()
                .where(ingestion_runs.c.id == run_id, ingestion_runs.c.status == "running")
                .values(status="failed", finished_at=finished_at, error_code=code.value)
            )
            if result.rowcount != 1:
                raise IngestionError(ErrorCode.DATABASE_ERROR)
