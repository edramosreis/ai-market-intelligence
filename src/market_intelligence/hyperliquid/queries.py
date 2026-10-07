"""Read-only repeatable snapshots and native funding/OI evidence."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from market_intelligence.config import ApiSettings
from market_intelligence.db.tables import funding_events as facts
from market_intelligence.db.tables import open_interest_snapshots as receipts
from market_intelligence.db.tables import perpetual_instruments as contracts
from market_intelligence.hyperliquid.models import (
    HISTORY_START,
    INSTRUMENT_CODE,
    SOURCE_CODE,
    hour,
    utc,
)
from market_intelligence.hyperliquid.query_models import (
    FundingCoverage,
    FundingLatest,
    FundingObservation,
    FundingPage,
    FundingProvenance,
    FundingSummary,
    MissingFundingRange,
    OpenInterestCoverage,
    OpenInterestLatest,
    OpenInterestObservation,
    OpenInterestPage,
    PerpetualInstrument,
    cursor_after,
    cursor_for,
)
from market_intelligence.ingestion.models import utc_now
from market_intelligence.queries.models import QueryValidationError

HOUR = timedelta(hours=1)


class HyperliquidQueries:
    def __init__(
        self,
        engine: Engine,
        settings: ApiSettings | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.engine, self.settings, self.now = engine, settings or ApiSettings(), now

    @contextmanager
    def snapshot(self) -> Iterator[Connection]:
        with (
            self.engine.connect().execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            ) as conn,
            conn.begin(),
        ):
            yield conn

    def window(
        self, start: datetime, end: datetime, *, funding: bool
    ) -> tuple[datetime, datetime, datetime]:
        try:
            start, end, retrieved = utc(start), utc(end), utc(self.now())
            if (
                start < HISTORY_START
                or start >= end
                or end - start > timedelta(days=self.settings.max_window_days)
            ):
                raise ValueError
            if funding:
                if start != hour(start) or end != hour(end) or end > hour(retrieved):
                    raise ValueError
            elif end > retrieved:
                raise ValueError
            return start, end, retrieved
        except ValueError, OverflowError:
            raise QueryValidationError(
                "Use bounded UTC windows from 2024; funding requires complete UTC hours, "
                "OI uses receipt time"
            ) from None

    @staticmethod
    def instrument(conn: Connection) -> PerpetualInstrument:
        row = (
            conn.execute(
                sa.select(contracts).where(
                    contracts.c.source_code == SOURCE_CODE, contracts.c.code == INSTRUMENT_CODE
                )
            )
            .mappings()
            .one()
        )
        return PerpetualInstrument(**row)

    @staticmethod
    def predicate(table: sa.Table, timestamp: Any, start: datetime, end: datetime) -> Any:
        return sa.and_(
            table.c.source_code == SOURCE_CODE,
            table.c.instrument_code == INSTRUMENT_CODE,
            timestamp >= start,
            timestamp < end,
        )

    @staticmethod
    def funding_observation(row: Mapping[Any, Any]) -> FundingObservation:
        return FundingObservation(
            event_at=row["event_at"],
            settlement_hour=row["settlement_hour"],
            funding_rate=row["funding_rate"],
            premium=row["premium"],
            provenance=FundingProvenance(
                first_ingested_at=row["first_ingested_at"],
                last_updated_at=row["last_updated_at"],
                last_ingestion_run_id=row["last_ingestion_run_id"],
            ),
        )

    @staticmethod
    def oi_observation(row: Mapping[Any, Any]) -> OpenInterestObservation:
        return OpenInterestObservation(
            **{
                name: row[name]
                for name in OpenInterestObservation.model_fields
                if name != "source_event_at"
            }
        )

    def funding_coverage(self, conn: Connection, start: datetime, end: datetime) -> FundingCoverage:
        predicate = self.predicate(facts, facts.c.event_at, start, end)
        count, first, last, last_hour = conn.execute(
            sa.select(
                sa.func.count(),
                sa.func.min(facts.c.event_at),
                sa.func.max(facts.c.event_at),
                sa.func.max(facts.c.settlement_hour),
            ).where(predicate)
        ).one()
        previous = sa.func.lag(facts.c.settlement_hour, 1, start - HOUR).over(
            order_by=facts.c.settlement_hour
        )
        sequence = (
            sa.select(facts.c.settlement_hour, previous.label("previous"))
            .where(predicate)
            .subquery()
        )
        interior = sa.select(
            (sequence.c.previous + HOUR).label("start"), sequence.c.settlement_hour.label("end")
        ).where(sequence.c.settlement_hour > sequence.c.previous + HOUR)
        tail_start = last_hour + HOUR if last_hour else start
        trailing = sa.select(
            sa.literal(tail_start, type_=sa.DateTime(timezone=True)).label("start"),
            sa.literal(end, type_=sa.DateTime(timezone=True)).label("end"),
        ).where(sa.literal(tail_start) < end)
        gaps = sa.union_all(interior, trailing).subquery()
        rows = (
            conn.execute(
                sa.select(gaps, sa.func.count().over().label("total"))
                .order_by(gaps.c.start)
                .limit(50)
            )
            .mappings()
            .all()
        )
        total = rows[0]["total"] if rows else 0
        expected = (end - start) // HOUR
        return FundingCoverage(
            status="no_data" if count == 0 else "complete" if count == expected else "incomplete",
            expected_hours=expected,
            observed_hours=count,
            missing_hours=expected - count,
            first_event_at=first,
            last_event_at=last,
            missing_ranges=[
                MissingFundingRange(
                    start=row["start"],
                    end=row["end"],
                    missing_hours=(row["end"] - row["start"]) // HOUR,
                )
                for row in rows
            ],
            missing_range_count=total,
            missing_ranges_truncated=total > 50,
        )

    def funding_page(
        self, start: datetime, end: datetime, limit: int = 200, cursor: str | None = None
    ) -> FundingPage:
        start, end, retrieved = self.window(start, end, funding=True)
        self.limit(limit)
        after = cursor_after(cursor, "funding", start, end)
        with self.snapshot() as conn:
            instrument = self.instrument(conn)
            coverage = self.funding_coverage(conn, start, end)
            statement = sa.select(facts).where(self.predicate(facts, facts.c.event_at, start, end))
            if after:
                statement = statement.where(facts.c.event_at > after[0])
            rows = (
                conn.execute(statement.order_by(facts.c.event_at).limit(limit + 1)).mappings().all()
            )
            page = rows[:limit]
            return FundingPage(
                instrument=instrument,
                retrieved_at=retrieved,
                start=start,
                end=end,
                coverage=coverage,
                events=[self.funding_observation(row) for row in page],
                next_cursor=cursor_for("funding", start, end, page[-1]["event_at"])
                if len(rows) > limit
                else None,
            )

    @staticmethod
    def limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise QueryValidationError("Page limit must be between 1 and 500")

    def funding_summary(self, start: datetime, end: datetime) -> FundingSummary:
        start, end, retrieved = self.window(start, end, funding=True)
        with self.snapshot() as conn:
            instrument = self.instrument(conn)
            coverage = self.funding_coverage(conn, start, end)
            total = percent = mean = None
            if coverage.status == "complete":
                total = conn.execute(
                    sa.select(sa.func.sum(facts.c.funding_rate)).where(
                        self.predicate(facts, facts.c.event_at, start, end)
                    )
                ).scalar_one()
                with localcontext() as context:
                    context.prec = 60
                    percent = total * 100
                    mean = (total / coverage.observed_hours).quantize(
                        Decimal("1e-18"), rounding=ROUND_HALF_EVEN
                    )
            return FundingSummary(
                instrument=instrument,
                retrieved_at=retrieved,
                start=start,
                end=end,
                coverage=coverage,
                rate_sum=total,
                rate_sum_percent=percent,
                mean_rate=mean,
            )

    def latest_funding(self) -> FundingLatest:
        retrieved = utc(self.now())
        with self.snapshot() as conn:
            instrument = self.instrument(conn)
            row = (
                conn.execute(
                    sa.select(facts)
                    .where(
                        facts.c.source_code == SOURCE_CODE,
                        facts.c.instrument_code == INSTRUMENT_CODE,
                        facts.c.event_at <= retrieved,
                    )
                    .order_by(facts.c.event_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            age = (
                (
                    Decimal((retrieved - row["event_at"]) // timedelta(microseconds=1))
                    / Decimal(1_000_000)
                )
                if row
                else None
            )
            threshold = self.settings.funding_stale_after_seconds
            return FundingLatest(
                instrument=instrument,
                retrieved_at=retrieved,
                event=self.funding_observation(row) if row else None,
                age_seconds=age,
                stale_after_seconds=threshold,
                status="no_data" if age is None else "stale" if age > threshold else "fresh",
            )

    def latest_open_interest(self) -> OpenInterestLatest:
        retrieved = utc(self.now())
        with self.snapshot() as conn:
            instrument = self.instrument(conn)
            row = (
                conn.execute(
                    sa.select(receipts)
                    .where(
                        receipts.c.source_code == SOURCE_CODE,
                        receipts.c.instrument_code == INSTRUMENT_CODE,
                        receipts.c.received_at <= retrieved,
                    )
                    .order_by(receipts.c.received_at.desc(), receipts.c.snapshot_id.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            age = (
                (
                    Decimal((retrieved - row["received_at"]) // timedelta(microseconds=1))
                    / Decimal(1_000_000)
                )
                if row
                else None
            )
            threshold = self.settings.open_interest_stale_after_seconds
            return OpenInterestLatest(
                instrument=instrument,
                retrieved_at=retrieved,
                snapshot=self.oi_observation(row) if row else None,
                age_seconds=age,
                stale_after_seconds=threshold,
                status="no_data" if age is None else "stale" if age > threshold else "fresh",
            )

    def open_interest_page(
        self, start: datetime, end: datetime, limit: int = 200, cursor: str | None = None
    ) -> OpenInterestPage:
        start, end, retrieved = self.window(start, end, funding=False)
        self.limit(limit)
        after = cursor_after(cursor, "open_interest", start, end)
        with self.snapshot() as conn:
            instrument = self.instrument(conn)
            predicate = self.predicate(receipts, receipts.c.received_at, start, end)
            count, first, last = conn.execute(
                sa.select(
                    sa.func.count(),
                    sa.func.min(receipts.c.received_at),
                    sa.func.max(receipts.c.received_at),
                ).where(predicate)
            ).one()
            statement = sa.select(receipts).where(predicate)
            if after:
                statement = statement.where(
                    sa.tuple_(receipts.c.received_at, receipts.c.snapshot_id)
                    > sa.tuple_(
                        sa.bindparam("after_received", after[0], type_=sa.DateTime(timezone=True)),
                        sa.bindparam("after_identity", after[1], type_=sa.UUID),
                    )
                )
            rows = (
                conn.execute(
                    statement.order_by(receipts.c.received_at, receipts.c.snapshot_id).limit(
                        limit + 1
                    )
                )
                .mappings()
                .all()
            )
            page = rows[:limit]
            return OpenInterestPage(
                instrument=instrument,
                retrieved_at=retrieved,
                start=start,
                end=end,
                coverage=OpenInterestCoverage(
                    observed_snapshots=count, first_received_at=first, last_received_at=last
                ),
                snapshots=[self.oi_observation(row) for row in page],
                next_cursor=cursor_for(
                    "open_interest", start, end, page[-1]["received_at"], page[-1]["snapshot_id"]
                )
                if len(rows) > limit
                else None,
            )
