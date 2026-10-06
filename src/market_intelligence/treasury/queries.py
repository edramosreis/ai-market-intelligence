"""Read-only snapshots of stored native Treasury curves and same-date yield spreads."""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from market_intelligence.config import ApiSettings
from market_intelligence.db.tables import treasury_ingestion_runs as runs
from market_intelligence.db.tables import treasury_yields as yields
from market_intelligence.ingestion.models import as_utc, utc_now
from market_intelligence.queries.models import QueryValidationError
from market_intelligence.treasury.models import (
    DATASET_CODE,
    SOURCE_CODE,
    TreasuryMonth,
    TreasuryTenor,
)
from market_intelligence.treasury.query_models import (
    TreasuryCoverage,
    TreasuryCurveEvidence,
    TreasuryCurvePage,
    TreasuryCurveResult,
    TreasuryMonthRead,
    TreasuryProvenance,
    TreasuryRateEvidence,
    TreasurySpread,
    treasury_cursor,
    treasury_cursor_after,
)


class TreasuryQueries:
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

    def validate_dates(self, start: date, end: date) -> datetime:
        retrieved = as_utc(self.now())
        if (
            type(start) is not date
            or type(end) is not date
            or start < date(1990, 1, 1)
            or start >= end
            or (end - start).days > self.settings.max_window_days
            or end > retrieved.date() + timedelta(days=1)
        ):
            raise QueryValidationError(
                "Use DATE bounds from 1990 with start < end, within the configured "
                "window and through today"
            )
        return retrieved

    def predicate(self, start: date, end: date) -> Any:
        return sa.and_(
            yields.c.source_code == SOURCE_CODE,
            yields.c.dataset_code == DATASET_CODE,
            yields.c.observed_on >= start,
            yields.c.observed_on < end,
        )

    def month_reads(
        self, conn: Connection, start: date, end: date
    ) -> dict[date, TreasuryMonthRead]:
        first = start.replace(day=1)
        rows = conn.execute(
            sa.select(runs)
            .where(
                runs.c.source_code == SOURCE_CODE,
                runs.c.dataset_code == DATASET_CODE,
                runs.c.requested_start >= first,
                runs.c.requested_start < end,
                runs.c.status == "succeeded",
            )
            .distinct(runs.c.requested_start)
            .order_by(runs.c.requested_start, runs.c.finished_at.desc(), runs.c.id.desc())
        ).mappings()
        return {
            row["requested_start"]: TreasuryMonthRead(
                run_id=row["id"],
                start=row["requested_start"],
                end=row["requested_end"],
                finished_at=row["finished_at"],
                received_dates=row["received_dates"],
                retained_dates=row["retained_dates"],
            )
            for row in rows
        }

    def curve_evidence(
        self,
        observed_on: date,
        rows: Sequence[Mapping[str, Any]],
        month_read: TreasuryMonthRead | None,
    ) -> TreasuryCurveEvidence:
        by_tenor = {row["tenor"]: row for row in rows}
        rates = []
        for tenor in TreasuryTenor:
            row = by_tenor.get(tenor)
            rates.append(
                TreasuryRateEvidence(
                    tenor=tenor,
                    yield_percent=row["yield_percent"] if row else None,
                    missing_reason=row["missing_reason"] if row else "not_stored",
                    provenance=TreasuryProvenance(
                        first_ingested_at=row["first_ingested_at"],
                        last_updated_at=row["last_updated_at"],
                        last_ingestion_run_id=row["last_ingestion_run_id"],
                    )
                    if row
                    else None,
                )
            )
        benchmarks = {rate.tenor: rate.yield_percent for rate in rates}
        missing = [
            tenor
            for tenor in (TreasuryTenor.TWO_YEARS, TreasuryTenor.TEN_YEARS)
            if benchmarks[tenor] is None
        ]
        points = basis = None
        if not missing:
            short, long = benchmarks[TreasuryTenor.TWO_YEARS], benchmarks[TreasuryTenor.TEN_YEARS]
            assert short is not None and long is not None
            with localcontext() as context:
                context.prec = 50
                points = long - short
                basis = points * Decimal(100)
        return TreasuryCurveEvidence(
            observed_on=observed_on,
            status="stored"
            if len(rows) == len(TreasuryTenor)
            else ("incomplete_stored_curve" if rows else "no_data"),
            stored_rates=len(rows),
            available_rates=sum(rate.yield_percent is not None for rate in rates),
            rates=rates,
            spread=TreasurySpread(
                status="unavailable" if missing else "available",
                percentage_points=points,
                basis_points=basis,
                missing_inputs=missing,
            ),
            latest_month_read=month_read,
        )

    def curve(self, observed_on: date) -> TreasuryCurveResult:
        if (
            type(observed_on) is not date
            or not date(1990, 1, 1) <= observed_on <= as_utc(self.now()).date()
        ):
            raise QueryValidationError("Use a Treasury source DATE from 1990 through today")
        end = observed_on + timedelta(days=1)
        retrieved = self.validate_dates(observed_on, end)
        with self.snapshot() as conn:
            rows = [
                dict(row)
                for row in (
                    conn.execute(sa.select(yields).where(self.predicate(observed_on, end)))
                    .mappings()
                    .all()
                )
            ]
            read = self.month_reads(conn, observed_on, end).get(
                TreasuryMonth(observed_on.year, observed_on.month).start
            )
            return TreasuryCurveResult(
                retrieved_at=retrieved, curve=self.curve_evidence(observed_on, rows, read)
            )

    def curve_page(
        self, start: date, end: date, limit: int = 20, cursor: str | None = None
    ) -> TreasuryCurvePage:
        retrieved = self.validate_dates(start, end)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise QueryValidationError("Treasury page limit must be between 1 and 100")
        after = treasury_cursor_after(cursor, start, end)
        with self.snapshot() as conn:
            summary = (
                conn.execute(
                    sa.select(
                        sa.func.count(sa.distinct(yields.c.observed_on)).label("dates"),
                        sa.func.count().label("rates"),
                        sa.func.count(yields.c.yield_percent).label("available"),
                        sa.func.count()
                        .filter(yields.c.missing_reason == "source_null")
                        .label("nulls"),
                        sa.func.count()
                        .filter(yields.c.missing_reason == "field_absent")
                        .label("absent"),
                        sa.func.min(yields.c.observed_on).label("first"),
                        sa.func.max(yields.c.observed_on).label("last"),
                    ).where(self.predicate(start, end))
                )
                .mappings()
                .one()
            )
            statement = (
                sa.select(yields.c.observed_on)
                .where(self.predicate(start, end))
                .distinct()
                .order_by(yields.c.observed_on)
                .limit(limit + 1)
            )
            if after:
                statement = statement.where(yields.c.observed_on > after)
            dates = conn.execute(statement).scalars().all()
            more = len(dates) > limit
            dates = dates[:limit]
            grouped: dict[date, list[Mapping[str, Any]]] = {day: [] for day in dates}
            reads: dict[date, TreasuryMonthRead] = {}
            if dates:
                for row in conn.execute(
                    sa.select(yields).where(
                        self.predicate(start, end), yields.c.observed_on.in_(dates)
                    )
                ).mappings():
                    grouped[row["observed_on"]].append(dict(row))
                reads = self.month_reads(conn, dates[0], dates[-1] + timedelta(days=1))
            return TreasuryCurvePage(
                retrieved_at=retrieved,
                start=start,
                end=end,
                coverage=TreasuryCoverage(
                    status="observations_stored" if summary["dates"] else "no_data",
                    observed_dates=summary["dates"],
                    stored_rates=summary["rates"],
                    available_rates=summary["available"],
                    source_null=summary["nulls"],
                    field_absent=summary["absent"],
                    first_observed_on=summary["first"],
                    last_observed_on=summary["last"],
                ),
                curves=[
                    self.curve_evidence(day, grouped[day], reads.get(day.replace(day=1)))
                    for day in dates
                ],
                next_cursor=treasury_cursor(start, end, dates[-1]) if more else None,
            )
