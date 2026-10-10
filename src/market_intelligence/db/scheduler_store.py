"""Short atomic dispatch transactions and a separate autocommit leader session."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.db.macro_tables import macro_ingestion_runs
from market_intelligence.db.scheduler_tables import scheduled_job_runs as runs
from market_intelligence.db.scheduler_tables import scheduler_job_state as states
from market_intelligence.db.tables import (
    funding_ingestion_runs,
    ingestion_runs,
    markets,
    open_interest_runs,
    treasury_ingestion_runs,
)
from market_intelligence.scheduler.jobs import JOBS, JobId, due_slot, utc
from market_intelligence.scheduler.models import ChildOutcome, OutcomeCode

SCHEDULER_LOCK_KEY = 5249801432534897


class SchedulerBusy(RuntimeError):
    pass


class LeadershipLost(RuntimeError):
    pass


@dataclass
class Leader:
    connection: Connection
    backend_id: int

    def healthy(self) -> bool:
        try:
            return bool(
                self.connection.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
                == self.backend_id
            )
        except SQLAlchemyError:
            return False

    def require(self) -> None:
        if not self.healthy():
            raise LeadershipLost("Scheduler ownership lost")


@dataclass(frozen=True)
class Attempt:
    id: UUID
    owner_id: UUID
    job: JobId
    started_at: datetime


class SchedulerStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def leader(self) -> Iterator[Leader]:
        with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            locked = conn.execute(
                sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": SCHEDULER_LOCK_KEY}
            ).scalar_one()
            if not locked:
                raise SchedulerBusy("Another scheduler owns dispatch")
            leader = Leader(conn, conn.execute(sa.text("SELECT pg_backend_pid()")).scalar_one())
            try:
                yield leader
            finally:
                if leader.healthy():
                    conn.execute(
                        sa.text("SELECT pg_advisory_unlock(:key)"), {"key": SCHEDULER_LOCK_KEY}
                    )
                else:
                    conn.invalidate()

    def recover(self, now: datetime) -> int:
        """Called only after leader acquisition, including jobs no longer selected."""
        now = utc(now)
        with self.engine.begin() as conn:
            # Use the same state-before-run lock order as admission/finalization/controls.
            conn.execute(sa.select(states).order_by(states.c.job_id).with_for_update()).all()
            unfinished = (
                conn.execute(sa.select(runs).where(runs.c.status == "running").with_for_update())
                .mappings()
                .all()
            )
            for row in unfinished:
                finished = max(now, row["started_at"])
                conn.execute(
                    runs.update()
                    .where(runs.c.id == row["id"])
                    .values(
                        status="uncertain",
                        finished_at=finished,
                        error_code=OutcomeCode.RECOVERED_UNFINISHED.value,
                    )
                )
                self._complete_state(
                    conn, JobId(row["job_id"]), finished, OutcomeCode.RECOVERED_UNFINISHED
                )
            return len(unfinished)

    def admit(self, job: JobId, owner_id: UUID, now: datetime) -> Attempt | None:
        now = utc(now)
        with self.engine.begin() as conn:
            state = (
                conn.execute(
                    sa.select(states).where(states.c.job_id == job.value).with_for_update()
                )
                .mappings()
                .one()
            )
            if state["initialized_at"] is None:
                conn.execute(
                    states.update()
                    .where(states.c.job_id == job.value)
                    .values(initialized_at=now, schedule_cursor=now)
                )
                return None
            if state["paused"]:
                return None
            if conn.execute(
                sa.select(runs.c.id).where(runs.c.job_id == job.value, runs.c.status == "running")
            ).first():
                return None
            due = due_slot(JOBS[job], state["schedule_cursor"], now, state["next_allowed_at"])
            if due is None:
                return None
            attempt = Attempt(uuid4(), owner_id, job, now)
            conn.execute(
                runs.insert().values(
                    id=attempt.id,
                    owner_id=owner_id,
                    job_id=job.value,
                    scheduled_for=due.slot,
                    definition_fingerprint=JOBS[job].fingerprint,
                    skipped_slots=due.skipped_slots,
                    started_at=now,
                    status="running",
                )
            )
            conn.execute(
                states.update()
                .where(states.c.job_id == job.value)
                .values(schedule_cursor=due.slot, last_dispatch_at=now)
            )
            return attempt

    def _valid_audits(
        self, conn: Connection, attempt: Attempt, outcome: ChildOutcome, finished: datetime
    ) -> bool:
        table, filters = self._audit_query(attempt.job)
        for reference in outcome.audits:
            row = (
                conn.execute(sa.select(table).where(table.c.id == reference.id, *filters))
                .mappings()
                .one_or_none()
            )
            if row is None or row["status"] != reference.status or row["finished_at"] is None:
                return False
            if not attempt.started_at <= row["started_at"] <= row["finished_at"] <= finished:
                return False
        return outcome.status != "succeeded" or (
            bool(outcome.audits)
            and all(a.status == "succeeded" for a in outcome.audits)
            and outcome.exit_code == 0
        )

    @staticmethod
    def _audit_query(job: JobId) -> tuple[sa.Table, tuple[sa.ColumnElement[bool], ...]]:
        if job == JobId.COINBASE:
            return ingestion_runs, (
                ingestion_runs.c.market_id.in_(
                    sa.select(markets.c.id).where(
                        markets.c.source_code == "coinbase_exchange",
                        markets.c.source_product_id == "BTC-USD",
                    )
                ),
                ingestion_runs.c.interval_seconds == 300,
            )
        if job == JobId.TREASURY:
            return treasury_ingestion_runs, (
                treasury_ingestion_runs.c.source_code == "us_treasury",
            )
        if job == JobId.FUNDING:
            return funding_ingestion_runs, (
                funding_ingestion_runs.c.source_code == "hyperliquid",
                funding_ingestion_runs.c.instrument_code == "BTC-PERP",
            )
        if job == JobId.OPEN_INTEREST:
            return open_interest_runs, (
                open_interest_runs.c.source_code == "hyperliquid",
                open_interest_runs.c.instrument_code == "BTC-PERP",
            )
        return macro_ingestion_runs, (
            macro_ingestion_runs.c.source_code
            == ("bls" if job == JobId.BLS else "federal_reserve_board"),
        )

    def finish(self, attempt: Attempt, outcome: ChildOutcome, now: datetime) -> ChildOutcome:
        with self.engine.begin() as conn:
            conn.execute(
                sa.select(states).where(states.c.job_id == attempt.job.value).with_for_update()
            ).one()
            row = (
                conn.execute(
                    sa.select(runs)
                    .where(
                        runs.c.id == attempt.id,
                        runs.c.owner_id == attempt.owner_id,
                        runs.c.job_id == attempt.job.value,
                        runs.c.status == "running",
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            finished = max(utc(now), row["started_at"])
            if not self._valid_audits(conn, attempt, outcome, finished):
                outcome = ChildOutcome("uncertain", OutcomeCode.AUDIT_MISMATCH, outcome.exit_code)
            conn.execute(
                runs.update()
                .where(runs.c.id == attempt.id)
                .values(
                    status=outcome.status,
                    finished_at=finished,
                    error_code=outcome.code.value if outcome.code else None,
                    exit_code=outcome.exit_code,
                    domain_audit_ids=[a.id for a in outcome.audits],
                )
            )
            self._complete_state(
                conn, attempt.job, finished, outcome.code if outcome.pause else None
            )
            return outcome

    @staticmethod
    def _complete_state(
        conn: Connection, job: JobId, finished: datetime, pause: OutcomeCode | None
    ) -> None:
        state = conn.execute(sa.select(states).where(states.c.job_id == job.value)).mappings().one()
        completion = max(finished, state["last_completion_at"] or finished)
        values: dict[str, Any] = {"last_completion_at": completion}
        if job == JobId.BLS:
            values["next_allowed_at"] = max(
                completion + timedelta(hours=24), state["next_allowed_at"] or completion
            )
        if pause is not None:
            values.update(paused=True, pause_code=pause.value)
        conn.execute(states.update().where(states.c.job_id == job.value).values(**values))

    def pause(self, job: JobId) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.select(states).where(states.c.job_id == job.value).with_for_update()
            ).one()
            # Preserve a stronger existing reason, especially unresolved completion.
            conn.execute(
                states.update()
                .where(states.c.job_id == job.value, states.c.paused.is_(False))
                .values(paused=True, pause_code=OutcomeCode.MANUAL_PAUSE.value)
            )

    def resume(self, job: JobId, now: datetime, reconcile_run: UUID | None = None) -> None:
        now = utc(now)
        with self.engine.begin() as conn:
            conn.execute(
                sa.select(states).where(states.c.job_id == job.value).with_for_update()
            ).one()
            if conn.execute(
                sa.select(runs.c.id).where(runs.c.job_id == job.value, runs.c.status == "running")
            ).first():
                raise ValueError("An unfinished run requires leader recovery first")
            unresolved = (
                conn.execute(
                    sa.select(runs)
                    .where(
                        runs.c.job_id == job.value,
                        runs.c.status == "uncertain",
                        runs.c.reconciled_at.is_(None),
                    )
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            if unresolved:
                if len(unresolved) != 1 or unresolved[0]["id"] != reconcile_run:
                    raise ValueError(
                        "Inspect native audits and explicitly reconcile the uncertain run"
                    )
                reconciled = max(now, unresolved[0]["finished_at"])
                conn.execute(
                    runs.update().where(runs.c.id == reconcile_run).values(reconciled_at=reconciled)
                )
                if job == JobId.BLS:
                    self._complete_state(conn, job, reconciled, None)
            elif reconcile_run is not None:
                raise ValueError("No matching uncertain run to reconcile")
            conn.execute(
                states.update()
                .where(states.c.job_id == job.value)
                .values(paused=False, pause_code=None)
            )

    def status(self, limit: int = 20) -> dict[str, object]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid scheduler status limit")
        with (
            self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
            conn.begin(),
        ):
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            return {
                "jobs": [
                    dict(r)
                    for r in conn.execute(sa.select(states).order_by(states.c.job_id)).mappings()
                ],
                "runs": [
                    dict(r)
                    for r in conn.execute(
                        sa.select(runs)
                        .order_by(runs.c.started_at.desc(), runs.c.id.desc())
                        .limit(limit)
                    ).mappings()
                ],
            }
