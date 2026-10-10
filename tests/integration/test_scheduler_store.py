"""Dispatch atomicity, recovery, least privilege and native audit validation on PostgreSQL."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError, ProgrammingError

from market_intelligence.cli import migration_config
from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.scheduler_store import LeadershipLost, SchedulerBusy, SchedulerStore
from market_intelligence.db.scheduler_tables import scheduled_job_runs as runs
from market_intelligence.db.scheduler_tables import scheduler_job_state as states
from market_intelligence.db.tables import open_interest_runs
from market_intelligence.scheduler.jobs import JOBS, JobId
from market_intelligence.scheduler.models import AuditReference, ChildOutcome, OutcomeCode

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 10, 22, tzinfo=UTC)


@pytest.fixture
def scheduler_store(
    database_settings: DatabaseSettings, admin_engine: Engine
) -> Iterator[SchedulerStore]:
    engine = create_db_engine(database_settings, DatabaseRole.INGEST)
    try:
        yield SchedulerStore(engine)
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(runs.delete())
            conn.execute(
                states.update().values(
                    initialized_at=None,
                    schedule_cursor=None,
                    paused=False,
                    pause_code=None,
                    last_dispatch_at=None,
                    last_completion_at=None,
                    next_allowed_at=None,
                )
            )
            conn.execute(open_interest_runs.delete())


def test_seeded_schema_matches_metadata(connection: Connection) -> None:
    assert set(connection.execute(sa.select(states.c.job_id)).scalars()) == {
        job.value for job in JobId
    }
    config = migration_config()
    config.attributes["connection"] = connection
    command.check(config)


def run_values() -> dict[str, Any]:
    return dict(
        id=uuid4(),
        owner_id=uuid4(),
        job_id="open_interest",
        scheduled_for=NOW,
        definition_fingerprint=JOBS[JobId.OPEN_INTEREST].fingerprint,
        skipped_slots=0,
        started_at=NOW,
        status="running",
    )


@pytest.mark.parametrize(
    "change",
    [
        {"job_id": "unknown"},
        {"scheduled_for": NOW + timedelta(seconds=1)},
        {"skipped_slots": -1},
        {"definition_fingerprint": "not_a_hash"},
        {"status": "succeeded", "finished_at": NOW, "exit_code": 0},
        {"status": "failed", "finished_at": NOW, "error_code": "raw failure"},
        {"domain_audit_ids": [None]},
        {"exit_code": 256},
        {"reconciled_at": NOW},
        {"finished_at": NOW},
        {"started_at": NOW - timedelta(seconds=1)},
    ],
)
def test_database_rejects_invalid_attempt_shapes(
    connection: Connection, change: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(runs.insert().values(**(run_values() | change)))


def test_unique_slot_and_single_unfinished_job(connection: Connection) -> None:
    values = run_values()
    connection.execute(runs.insert().values(**values))
    for change in (
        {"id": uuid4()},
        {
            "id": uuid4(),
            "scheduled_for": NOW + timedelta(minutes=15),
            "started_at": NOW + timedelta(minutes=15),
        },
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(runs.insert().values(**(values | change)))


@pytest.mark.parametrize("role", [DatabaseRole.INGEST, DatabaseRole.READ])
def test_restricted_grants(database_settings: DatabaseSettings, role: DatabaseRole) -> None:
    engine = create_db_engine(database_settings, role)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.select(sa.func.count()).select_from(states)).scalar_one() == 6
            conn.rollback()
            denied = [states.insert().values(job_id="coinbase"), states.delete(), runs.delete()]
            if role == DatabaseRole.READ:
                denied += [
                    states.update().values(paused=False),
                    runs.insert().values(**run_values()),
                    runs.update().values(status="running"),
                ]
            for statement in denied:
                with pytest.raises(ProgrammingError), conn.begin():
                    conn.execute(statement)
    finally:
        engine.dispose()


def test_enrollment_coalescing_atomic_cursor_and_rollback(scheduler_store: SchedulerStore) -> None:
    owner = uuid4()
    assert scheduler_store.admit(JobId.OPEN_INTEREST, owner, NOW) is None
    assert scheduler_store.admit(JobId.OPEN_INTEREST, owner, NOW + timedelta(minutes=14)) is None
    attempt = scheduler_store.admit(JobId.OPEN_INTEREST, owner, NOW + timedelta(hours=12))
    assert attempt is not None
    status = scheduler_store.status()
    assert status["runs"][0]["skipped_slots"] == 47  # type: ignore[index]
    assert scheduler_store.admit(JobId.OPEN_INTEREST, owner, NOW + timedelta(days=1)) is None
    scheduler_store.finish(
        attempt, ChildOutcome("failed", OutcomeCode.CHILD_FAILED, 1), NOW + timedelta(hours=12)
    )
    assert scheduler_store.admit(JobId.OPEN_INTEREST, owner, NOW + timedelta(hours=11)) is None
    assert scheduler_store.admit(JobId.OPEN_INTEREST, owner, NOW + timedelta(hours=12)) is None


def test_native_audit_reference_verified_and_manual_pause_survives_completion(
    scheduler_store: SchedulerStore, admin_engine: Engine
) -> None:
    scheduler_store.admit(JobId.OPEN_INTEREST, uuid4(), NOW)
    started = NOW + timedelta(minutes=15)
    attempt = scheduler_store.admit(JobId.OPEN_INTEREST, uuid4(), started)
    assert attempt is not None
    audit_id = uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            open_interest_runs.insert().values(
                id=audit_id,
                source_code="hyperliquid",
                instrument_code="BTC-PERP",
                started_at=started,
                finished_at=started + timedelta(seconds=1),
                status="succeeded",
                received=1,
            )
        )
    scheduler_store.pause(JobId.OPEN_INTEREST)
    outcome = scheduler_store.finish(
        attempt,
        ChildOutcome("succeeded", exit_code=0, audits=(AuditReference(audit_id, "succeeded"),)),
        started + timedelta(seconds=2),
    )
    assert outcome.status == "succeeded"
    with admin_engine.connect() as conn:
        assert conn.execute(
            sa.select(states.c.paused).where(states.c.job_id == "open_interest")
        ).scalar_one()
        assert conn.execute(sa.select(runs.c.domain_audit_ids)).scalar_one() == [audit_id]


def test_unknown_audit_pauses_instead_of_certifying_success(
    scheduler_store: SchedulerStore,
) -> None:
    scheduler_store.admit(JobId.OPEN_INTEREST, uuid4(), NOW)
    attempt = scheduler_store.admit(JobId.OPEN_INTEREST, uuid4(), NOW + timedelta(minutes=15))
    assert attempt is not None
    outcome = scheduler_store.finish(
        attempt,
        ChildOutcome("succeeded", exit_code=0, audits=(AuditReference(uuid4(), "succeeded"),)),
        NOW + timedelta(minutes=16),
    )
    assert outcome.code == OutcomeCode.AUDIT_MISMATCH
    with pytest.raises(ValueError):
        scheduler_store.resume(JobId.OPEN_INTEREST, NOW + timedelta(minutes=17))
    scheduler_store.resume(JobId.OPEN_INTEREST, NOW + timedelta(minutes=17), attempt.id)
    assert scheduler_store.admit(JobId.OPEN_INTEREST, uuid4(), NOW + timedelta(minutes=17)) is None
    assert scheduler_store.status()["runs"][0]["status"] == "uncertain"  # type: ignore[index]


def test_recovery_is_durable_and_bls_reconciliation_preserves_cooldown(
    scheduler_store: SchedulerStore,
) -> None:
    scheduler_store.admit(JobId.BLS, uuid4(), NOW)
    attempt = scheduler_store.admit(JobId.BLS, uuid4(), NOW + timedelta(hours=1))
    assert attempt is not None
    with pytest.raises(ValueError):
        scheduler_store.resume(JobId.BLS, NOW)
    assert scheduler_store.recover(NOW + timedelta(hours=2)) == 1
    assert scheduler_store.recover(NOW + timedelta(hours=3)) == 0
    with pytest.raises(ValueError):
        scheduler_store.resume(JobId.BLS, NOW + timedelta(hours=3), uuid4())
    scheduler_store.resume(JobId.BLS, NOW + timedelta(hours=3), attempt.id)
    assert scheduler_store.admit(JobId.BLS, uuid4(), NOW + timedelta(days=1, hours=2)) is None
    assert scheduler_store.admit(JobId.BLS, uuid4(), NOW + timedelta(days=1, hours=3)) is not None


def test_bls_rejection_pause_resume_cannot_reset_cooldown(scheduler_store: SchedulerStore) -> None:
    scheduler_store.admit(JobId.BLS, uuid4(), NOW)
    attempt = scheduler_store.admit(JobId.BLS, uuid4(), NOW + timedelta(hours=1))
    assert attempt is not None
    scheduler_store.finish(
        attempt,
        ChildOutcome("failed", OutcomeCode.SOURCE_REJECTED, 1),
        NOW + timedelta(hours=1, minutes=2),
    )
    assert scheduler_store.admit(JobId.BLS, uuid4(), NOW + timedelta(days=2)) is None
    scheduler_store.pause(JobId.BLS)
    scheduler_store.resume(JobId.BLS, NOW + timedelta(hours=2))
    assert scheduler_store.admit(JobId.BLS, uuid4(), NOW + timedelta(days=1, hours=1)) is None
    assert (
        scheduler_store.admit(JobId.BLS, uuid4(), NOW + timedelta(days=1, hours=1, minutes=2))
        is not None
    )


def test_leader_excludes_second_runner_without_idle_transaction(
    scheduler_store: SchedulerStore, admin_engine: Engine
) -> None:
    with scheduler_store.leader() as leader:
        assert leader.healthy()
        with admin_engine.connect() as conn:
            assert (
                conn.execute(
                    sa.text("SELECT state FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": leader.backend_id},
                ).scalar_one()
                != "idle in transaction"
            )
        with pytest.raises(SchedulerBusy), scheduler_store.leader():
            pytest.fail("Duplicate leader admitted")
    with scheduler_store.leader() as successor:
        successor.require()


def test_database_loss_detected_and_unfinished_attempt_recovered(
    scheduler_store: SchedulerStore, admin_engine: Engine
) -> None:
    scheduler_store.admit(JobId.OPEN_INTEREST, uuid4(), NOW)
    attempt = scheduler_store.admit(JobId.OPEN_INTEREST, uuid4(), NOW + timedelta(minutes=15))
    assert attempt is not None
    with scheduler_store.leader() as leader:
        with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": leader.backend_id})
        assert not leader.healthy()
        with pytest.raises(LeadershipLost):
            leader.require()
    with scheduler_store.leader():
        assert scheduler_store.recover(NOW + timedelta(minutes=16)) == 1
    assert scheduler_store.admit(JobId.OPEN_INTEREST, uuid4(), NOW + timedelta(days=1)) is None
