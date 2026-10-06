"""Current Treasury facts and atomic monthly audits, with independent writer exclusion."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.db.tables import treasury_ingestion_runs as runs
from market_intelligence.db.tables import treasury_yields as yields
from market_intelligence.ingestion.models import as_utc
from market_intelligence.treasury.models import (
    DATASET_CODE,
    SOURCE_CODE,
    TreasuryCurve,
    TreasuryErrorCode,
    TreasuryIngestionError,
    TreasuryMissingReason,
    TreasuryMonth,
    TreasuryReport,
    TreasuryTenor,
)

TREASURY_LOCK_KEY = 199014


class TreasuryStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def exclusive_writer(self) -> Iterator[None]:
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            if not conn.execute(
                sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": TREASURY_LOCK_KEY}
            ).scalar_one():
                raise TreasuryIngestionError(TreasuryErrorCode.CONCURRENT_JOB)
            try:
                yield
            finally:
                try:
                    conn.execute(
                        sa.text("SELECT pg_advisory_unlock(:key)"), {"key": TREASURY_LOCK_KEY}
                    )
                except SQLAlchemyError:
                    conn.invalidate()

    def completed(self, month: TreasuryMonth) -> TreasuryReport | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    sa.select(runs)
                    .where(
                        runs.c.source_code == SOURCE_CODE,
                        runs.c.dataset_code == DATASET_CODE,
                        runs.c.requested_start == month.start,
                        runs.c.requested_end == month.end,
                        runs.c.status == "succeeded",
                    )
                    .order_by(runs.c.finished_at.desc(), runs.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            dates = conn.execute(
                sa.select(yields.c.observed_on, sa.func.count().label("rates"))
                .where(
                    yields.c.source_code == SOURCE_CODE,
                    yields.c.dataset_code == DATASET_CODE,
                    yields.c.observed_on >= month.start,
                    yields.c.observed_on < month.end,
                )
                .group_by(yields.c.observed_on)
            ).all()
            if len(dates) != row["received_dates"] + row["retained_dates"] or any(
                row.rates != len(TreasuryTenor) for row in dates
            ):
                return None
            return TreasuryReport(
                row["id"],
                month,
                row["received_dates"],
                row["received_rates"],
                row["inserted"],
                row["updated"],
                row["unchanged"],
                row["source_null"],
                row["field_absent"],
                row["retained_dates"],
                True,
            )

    def start_run(self, month: TreasuryMonth, started_at: datetime) -> UUID:
        run_id = uuid4()
        with self.engine.begin() as conn:
            conn.execute(
                runs.insert().values(
                    id=run_id,
                    source_code=SOURCE_CODE,
                    dataset_code=DATASET_CODE,
                    requested_start=month.start,
                    requested_end=month.end,
                    started_at=as_utc(started_at),
                    status="running",
                )
            )
        return run_id

    def persist(
        self,
        run_id: UUID,
        month: TreasuryMonth,
        curves: Sequence[TreasuryCurve],
        finished_at: datetime,
    ) -> TreasuryReport:
        observed_dates = {curve.observed_on for curve in curves}
        if len(observed_dates) != len(curves) or any(
            not month.start <= day < month.end for day in observed_dates
        ):
            raise TreasuryIngestionError(TreasuryErrorCode.INVALID_PAYLOAD)
        finished_at = as_utc(finished_at)
        with self.engine.begin() as conn:
            run = (
                conn.execute(
                    sa.select(runs)
                    .where(
                        runs.c.id == run_id,
                        runs.c.source_code == SOURCE_CODE,
                        runs.c.dataset_code == DATASET_CODE,
                        runs.c.status == "running",
                        runs.c.requested_start == month.start,
                        runs.c.requested_end == month.end,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if run is None or finished_at < run["started_at"]:
                raise TreasuryIngestionError(TreasuryErrorCode.DATABASE_ERROR)
            existing = {
                (row["observed_on"], row["tenor"]): (row["yield_percent"], row["missing_reason"])
                for row in conn.execute(
                    sa.select(yields).where(
                        yields.c.source_code == SOURCE_CODE,
                        yields.c.dataset_code == DATASET_CODE,
                        yields.c.observed_on >= month.start,
                        yields.c.observed_on < month.end,
                    )
                ).mappings()
            }
            inserted = updated = unchanged = source_null = field_absent = 0
            changes = []
            for curve in curves:
                for rate in curve.rates:
                    source_null += rate.missing_reason == TreasuryMissingReason.SOURCE_NULL
                    field_absent += rate.missing_reason == TreasuryMissingReason.FIELD_ABSENT
                    key = (curve.observed_on, rate.tenor.value)
                    value = (rate.yield_percent, rate.missing_reason)
                    if key in existing and existing[key] == value:
                        unchanged += 1
                        continue
                    if key in existing:
                        updated += 1
                    else:
                        inserted += 1
                    changes.append(
                        {
                            "source_code": SOURCE_CODE,
                            "dataset_code": DATASET_CODE,
                            "observed_on": curve.observed_on,
                            "tenor": rate.tenor.value,
                            "yield_percent": rate.yield_percent,
                            "missing_reason": rate.missing_reason,
                            "first_ingested_at": finished_at,
                            "last_updated_at": finished_at,
                            "last_ingestion_run_id": run_id,
                        }
                    )
            if changes:
                statement = insert(yields).values(changes)  # at most 31 * 14 rows
                excluded = statement.excluded
                conn.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            yields.c.source_code,
                            yields.c.dataset_code,
                            yields.c.observed_on,
                            yields.c.tenor,
                        ],
                        set_={
                            "yield_percent": excluded.yield_percent,
                            "missing_reason": excluded.missing_reason,
                            "last_updated_at": sa.func.greatest(
                                excluded.last_updated_at, yields.c.last_updated_at
                            ),
                            "last_ingestion_run_id": excluded.last_ingestion_run_id,
                        },
                        where=sa.or_(
                            yields.c.yield_percent.is_distinct_from(excluded.yield_percent),
                            yields.c.missing_reason.is_distinct_from(excluded.missing_reason),
                        ),
                    )
                )
            retained_dates = len({key[0] for key in existing} - observed_dates)
            report = TreasuryReport(
                run_id,
                month,
                len(curves),
                len(curves) * len(TreasuryTenor),
                inserted,
                updated,
                unchanged,
                source_null,
                field_absent,
                retained_dates,
            )
            conn.execute(
                runs.update()
                .where(runs.c.id == run_id)
                .values(
                    status="succeeded",
                    finished_at=finished_at,
                    received_dates=report.received_dates,
                    received_rates=report.received_rates,
                    inserted=inserted,
                    updated=updated,
                    unchanged=unchanged,
                    source_null=source_null,
                    field_absent=field_absent,
                    retained_dates=retained_dates,
                )
            )
        return report

    def fail_run(self, run_id: UUID, finished_at: datetime, code: TreasuryErrorCode) -> None:
        with self.engine.begin() as conn:
            result = conn.execute(
                runs.update()
                .where(runs.c.id == run_id, runs.c.status == "running")
                .values(status="failed", finished_at=as_utc(finished_at), error_code=code.value)
            )
            if result.rowcount != 1:
                raise TreasuryIngestionError(TreasuryErrorCode.DATABASE_ERROR)
