"""PostgreSQL calculations and coverage from a consistent read-only snapshot."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.postgresql import aggregate_order_by

from market_intelligence.config import ApiSettings
from market_intelligence.db.macro_tables import (
    macro_current,
    macro_ingestion_runs,
    macro_observed_versions,
    macro_series,
    macro_version_footnotes,
)
from market_intelligence.db.tables import (
    candles,
    data_sources,
    funding_events,
    markets,
    open_interest_snapshots,
    treasury_ingestion_runs,
    treasury_yields,
)
from market_intelligence.ingestion.models import (
    INTERVAL,
    TimeWindow,
    as_utc,
    closed_cutoff,
    utc_now,
)
from market_intelligence.queries.models import (
    CandleEvidence,
    CandlePage,
    Coverage,
    Latest,
    Market,
    MissingRange,
    Provenance,
    QueryValidationError,
    Summary,
    UnknownMarketError,
    cursor_after,
    cursor_for,
    validate_window,
)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
EXPECTED_REVISION = "0004"


class MarketQueries:
    def __init__(
        self,
        engine: Engine,
        settings: ApiSettings | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.engine = engine
        self.settings = settings or ApiSettings()
        self.now = now

    @contextmanager
    def snapshot(self) -> Iterator[Connection]:
        with (
            self.engine.connect().execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            ) as conn,
            conn.begin(),
        ):
            yield conn

    def ready(self) -> bool:
        with self.snapshot() as conn:
            revisions = (
                conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
            )
            conn.execute(sa.select(markets.c.id).limit(1)).all()
            conn.execute(sa.select(treasury_yields.c.observed_on).limit(1)).all()
            conn.execute(sa.select(treasury_ingestion_runs.c.id).limit(1)).all()
            conn.execute(sa.select(funding_events.c.event_at).limit(1)).all()
            conn.execute(sa.select(open_interest_snapshots.c.snapshot_id).limit(1)).all()
            for table in (
                macro_series,
                macro_ingestion_runs,
                macro_observed_versions,
                macro_version_footnotes,
                macro_current,
            ):
                conn.execute(sa.select(table).limit(1)).all()
            return revisions == [EXPECTED_REVISION]

    def predicate(self, market_id: int, cutoff: datetime, window: TimeWindow | None = None) -> Any:
        conditions = [
            candles.c.market_id == market_id,
            candles.c.interval_seconds == 300,
            candles.c.opened_at < cutoff,
        ]
        if window:
            conditions += [candles.c.opened_at >= window.start, candles.c.opened_at < window.end]
        return sa.and_(*conditions)

    def market(self, conn: Connection, market_id: int, cutoff: datetime) -> Market:
        row = (
            conn.execute(
                sa.select(markets, data_sources.c.name.label("source_name"))
                .join(data_sources, markets.c.source_code == data_sources.c.code)
                .where(markets.c.id == market_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise UnknownMarketError("Unknown market")
        extent = conn.execute(
            sa.select(sa.func.min(candles.c.opened_at), sa.func.max(candles.c.opened_at)).where(
                self.predicate(market_id, cutoff)
            )
        ).one()
        return Market(**row, earliest_opened_at=extent[0], latest_opened_at=extent[1])

    def list_markets(self) -> list[Market]:
        cutoff = closed_cutoff(self.now())
        with self.snapshot() as conn:
            ids = conn.execute(sa.select(markets.c.id).order_by(markets.c.id)).scalars().all()
            return [self.market(conn, market_id, cutoff) for market_id in ids]

    def coverage(
        self, conn: Connection, market_id: int, window: TimeWindow, cutoff: datetime
    ) -> Coverage:
        predicate = self.predicate(market_id, cutoff, window)
        count, first, last = conn.execute(
            sa.select(
                sa.func.count(), sa.func.min(candles.c.opened_at), sa.func.max(candles.c.opened_at)
            ).where(predicate)
        ).one()
        # Lag exposes interior/leading gaps without materializing a generated time grid.
        previous = sa.func.lag(candles.c.opened_at, 1, window.start - INTERVAL).over(
            order_by=candles.c.opened_at
        )
        sequence = (
            sa.select(candles.c.opened_at, previous.label("previous")).where(predicate).subquery()
        )
        interior = sa.select(
            (sequence.c.previous + INTERVAL).label("start"), sequence.c.opened_at.label("end")
        ).where(sequence.c.opened_at > sequence.c.previous + INTERVAL)
        trailing_start = (last + INTERVAL) if last else window.start
        trailing = sa.select(
            sa.literal(trailing_start, type_=sa.DateTime(timezone=True)).label("start"),
            sa.literal(window.end, type_=sa.DateTime(timezone=True)).label("end"),
        ).where(sa.literal(trailing_start) < window.end)
        gaps = sa.union_all(interior, trailing).subquery()
        rows = (
            conn.execute(
                sa.select(gaps, sa.func.count().over().label("total_ranges"))
                .order_by(gaps.c.start)
                .limit(50)
            )
            .mappings()
            .all()
        )
        total_ranges = rows[0]["total_ranges"] if rows else 0
        with localcontext() as context:
            context.prec = 60
            ratio = (Decimal(count) / Decimal(window.expected)).quantize(
                Decimal("0.00000001"), rounding=ROUND_HALF_EVEN
            )
        status: Literal["complete", "incomplete", "no_data"] = (
            "no_data" if count == 0 else "complete" if count == window.expected else "incomplete"
        )
        return Coverage(
            status=status,
            expected_buckets=window.expected,
            actual_buckets=count,
            missing_buckets=window.expected - count,
            ratio=ratio,
            observed_start=first,
            observed_end=last + INTERVAL if last else None,
            missing_ranges=[
                MissingRange(
                    start=row["start"],
                    end=row["end"],
                    missing_buckets=(row["end"] - row["start"]) // INTERVAL,
                )
                for row in rows
            ],
            missing_range_count=total_ranges,
            missing_ranges_truncated=total_ranges > 50,
        )

    @staticmethod
    def provenance(row: Mapping[str, Any], canonical: bool = False) -> Provenance:
        return Provenance(
            first_ingested_at=row["first_ingested_at"],
            last_updated_at=row["last_updated_at"],
            ingestion_run_count=row["ingestion_run_count"],
            last_ingestion_run_id=row.get("last_ingestion_run_id") if canonical else None,
        )

    @staticmethod
    def aggregate_columns() -> list[Any]:
        def ordered(field: Any, descending: bool = False) -> Any:
            order = candles.c.opened_at.desc() if descending else candles.c.opened_at.asc()
            return sa.func.array_agg(aggregate_order_by(field, order), type_=sa.ARRAY(field.type))[
                1
            ]

        return [
            sa.func.count().label("actual_constituents"),
            ordered(candles.c.open).label("open"),
            ordered(candles.c.close, True).label("close"),
            sa.func.max(candles.c.high).label("high"),
            sa.func.min(candles.c.low).label("low"),
            sa.func.sum(candles.c.base_volume).label("base_volume"),
            sa.func.min(candles.c.first_ingested_at).label("first_ingested_at"),
            sa.func.max(candles.c.last_updated_at).label("last_updated_at"),
            sa.func.count(sa.distinct(candles.c.last_ingestion_run_id)).label(
                "ingestion_run_count"
            ),
            ordered(candles.c.last_ingestion_run_id, True).label("last_ingestion_run_id"),
        ]

    def candle(self, row: Mapping[str, Any], resolution: int) -> CandleEvidence:
        expected = resolution // 300
        complete = row["actual_constituents"] == expected
        return CandleEvidence(
            opened_at=row["opened_at"],
            ended_at=row["opened_at"] + timedelta(seconds=resolution),
            interval_seconds=resolution,
            expected_constituents=expected,
            actual_constituents=row["actual_constituents"],
            status="complete" if complete else "incomplete",
            **{
                field: row[field] if complete else None
                for field in ("open", "high", "low", "close", "base_volume")
            },
            provenance=self.provenance(row, resolution == 300),
        )

    def candle_page(
        self,
        market_id: int,
        start: datetime,
        end: datetime,
        resolution: int = 300,
        limit: int = 200,
        cursor: str | None = None,
    ) -> CandlePage:
        window = validate_window(start, end, resolution, self.settings.max_window_days)
        if not 1 <= limit <= 500:
            raise QueryValidationError("Page limit must be between 1 and 500")
        after = cursor_after(cursor, market_id, window, resolution) if cursor is not None else None
        retrieved = as_utc(self.now())
        cutoff = closed_cutoff(retrieved)
        bucket = sa.func.date_bin(
            sa.cast(sa.literal(timedelta(seconds=resolution)), sa.Interval),
            candles.c.opened_at,
            EPOCH,
        )
        predicate = self.predicate(market_id, cutoff, window)
        if after is not None:
            predicate = sa.and_(
                predicate, candles.c.opened_at >= after + timedelta(seconds=resolution)
            )
        with self.snapshot() as conn:
            market = self.market(conn, market_id, cutoff)
            coverage = self.coverage(conn, market_id, window, cutoff)
            if resolution == 300:
                statement = (
                    sa.select(
                        candles,
                        sa.literal(1).label("actual_constituents"),
                        sa.literal(1).label("ingestion_run_count"),
                    )
                    .where(predicate)
                    .order_by(candles.c.opened_at)
                    .limit(limit + 1)
                )
                rows = conn.execute(statement).mappings().all()
            else:
                # Bound aggregate arrays to the next page's groups, even for multi-year windows.
                page_buckets = (
                    conn.execute(
                        sa.select(bucket)
                        .where(predicate)
                        .distinct()
                        .order_by(bucket)
                        .limit(limit + 1)
                    )
                    .scalars()
                    .all()
                )
                rows = []
                if page_buckets:
                    rows = (
                        conn.execute(
                            sa.select(bucket.label("opened_at"), *self.aggregate_columns())
                            .where(
                                predicate,
                                candles.c.opened_at
                                < page_buckets[-1] + timedelta(seconds=resolution),
                            )
                            .group_by(bucket)
                            .order_by(bucket)
                        )
                        .mappings()
                        .all()
                    )
            selected = rows[:limit]
            next_cursor = (
                cursor_for(market_id, window, resolution, selected[-1]["opened_at"])
                if len(rows) > limit
                else None
            )
            return CandlePage(
                market=market,
                start=window.start,
                end=window.end,
                retrieved_at=retrieved,
                coverage=coverage,
                output_interval_seconds=resolution,
                candles=[self.candle(dict(row), resolution) for row in selected],
                next_cursor=next_cursor,
            )

    def summary(self, market_id: int, start: datetime, end: datetime) -> Summary:
        window = validate_window(start, end, 300, self.settings.max_window_days)
        retrieved = as_utc(self.now())
        cutoff = closed_cutoff(retrieved)
        with self.snapshot() as conn:
            market = self.market(conn, market_id, cutoff)
            coverage = self.coverage(conn, market_id, window, cutoff)
            # Summary aggregates avoid ordered arrays proportional to historical window length.
            predicate = self.predicate(market_id, cutoff, window)
            row = (
                conn.execute(
                    sa.select(
                        sa.func.max(candles.c.high).label("high"),
                        sa.func.min(candles.c.low).label("low"),
                        sa.func.sum(candles.c.base_volume).label("base_volume"),
                        sa.func.min(candles.c.first_ingested_at).label("first_ingested_at"),
                        sa.func.max(candles.c.last_updated_at).label("last_updated_at"),
                        sa.func.count(sa.distinct(candles.c.last_ingestion_run_id)).label(
                            "ingestion_run_count"
                        ),
                    ).where(predicate)
                )
                .mappings()
                .one()
            )
            prices: dict[str, Any] = dict.fromkeys(
                (
                    "opening_price",
                    "closing_price",
                    "open_to_close_return_percent",
                    "high",
                    "low",
                    "base_volume",
                )
            )
            if coverage.status == "complete":
                opening = conn.execute(
                    sa.select(candles.c.open).where(predicate, candles.c.opened_at == window.start)
                ).scalar_one()
                closing = conn.execute(
                    sa.select(candles.c.close).where(
                        predicate, candles.c.opened_at == window.end - INTERVAL
                    )
                ).scalar_one()
                with localcontext() as context:
                    context.prec = 80
                    change = ((closing / opening - 1) * 100).quantize(
                        Decimal("0.00000001"), rounding=ROUND_HALF_EVEN
                    )
                prices.update(
                    opening_price=opening,
                    closing_price=closing,
                    open_to_close_return_percent=change,
                    **{name: row[name] for name in ("high", "low", "base_volume")},
                )
            return Summary(
                market=market,
                start=window.start,
                end=window.end,
                retrieved_at=retrieved,
                coverage=coverage,
                provenance=self.provenance(dict(row)),
                **prices,
            )

    def latest(self, market_id: int) -> Latest:
        retrieved = as_utc(self.now())
        cutoff = closed_cutoff(retrieved)
        with self.snapshot() as conn:
            market = self.market(conn, market_id, cutoff)
            row = (
                conn.execute(
                    sa.select(candles)
                    .where(self.predicate(market_id, cutoff))
                    .order_by(candles.c.opened_at.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            candle = None
            age = None
            if row is not None:
                candle = self.candle(
                    {**row, "actual_constituents": 1, "ingestion_run_count": 1}, 300
                )
                elapsed = retrieved - candle.ended_at
                age = Decimal(elapsed.days * 86400 + elapsed.seconds) + Decimal(
                    elapsed.microseconds
                ) / Decimal(1000000)
            return Latest(
                market=market,
                status="available" if candle else "no_data",
                retrieved_at=retrieved,
                candle=candle,
                age_seconds=age,
                stale=age > self.settings.stale_after_seconds if age is not None else None,
                stale_after_seconds=self.settings.stale_after_seconds,
            )
