"""Dispatch atomicity, recovery, least privilege and native audit validation on PostgreSQL."""

from collections.abc import Callable, Iterator
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
from market_intelligence.scheduler.jobs import JOBS, JobDefinition, JobId, SchedulerSettings
from market_intelligence.scheduler.models import AuditReference, ChildOutcome, OutcomeCode
from market_intelligence.scheduler.service import SchedulerService

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 10, 22, tzinfo=UTC)


@pytest.fixture
def scheduler_store(
    database_settings: DatabaseSettings, admin_engine: Engine
) -> Iterator[SchedulerStore]:
    engine = create_db_engine(database_settings, DatabaseRole.INGEST)
    with admin_engine.connect() as conn:
        existing_oi = tuple(conn.execute(sa.select(open_interest_runs.c.id)).scalars())
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
            conn.execute(
                open_interest_runs.delete().where(open_interest_runs.c.id.not_in(existing_oi))
            )


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


def test_admission_cursor_and_intent_roll_back_together(scheduler_store: SchedulerStore) -> None:
    scheduler_store.admit(JobId.OPEN_INTEREST, uuid4(), NOW)

    def fail_update(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.startswith("UPDATE scheduler_job_state"):
            raise RuntimeError("Synthetic failure after intent insertion")

    sa.event.listen(scheduler_store.engine, "before_cursor_execute", fail_update)
    try:
        with pytest.raises(RuntimeError):
            scheduler_store.admit(JobId.OPEN_INTEREST, uuid4(), NOW + timedelta(minutes=15))
    finally:
        sa.event.remove(scheduler_store.engine, "before_cursor_execute", fail_update)
    with scheduler_store.engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(runs)).scalar_one() == 0
        assert (
            conn.execute(
                sa.select(states.c.schedule_cursor).where(states.c.job_id == "open_interest")
            ).scalar_one()
            == NOW
        )
    assert (
        scheduler_store.admit(JobId.OPEN_INTEREST, uuid4(), NOW + timedelta(minutes=15)) is not None
    )


def test_service_first_enrollment_then_fixed_sequential_children_without_transactions(
    scheduler_store: SchedulerStore,
    admin_engine: Engine,
    database_settings: DatabaseSettings,
) -> None:
    clock = [NOW]
    called: list[JobId] = []

    class SyntheticRunner:
        def run(
            self,
            job: JobDefinition,
            settings: DatabaseSettings,
            healthy: Callable[[], bool],
            stopped: Callable[[], bool],
        ) -> ChildOutcome:
            assert healthy() and not stopped()
            with admin_engine.connect() as conn:
                assert (
                    conn.execute(
                        sa.text(
                            "SELECT count(*) FROM pg_stat_activity WHERE usename = :writer "
                            "AND state = 'idle in transaction'"
                        ),
                        {"writer": settings.ingest_user},
                    ).scalar_one()
                    == 0
                )
                assert (
                    conn.execute(
                        sa.select(runs.c.status)
                        .where(runs.c.job_id == job.id.value)
                        .order_by(runs.c.started_at.desc())
                        .limit(1)
                    ).scalar_one()
                    == "running"
                )
            called.append(job.id)
            return ChildOutcome("failed", OutcomeCode.CHILD_FAILED, 1)

    service = SchedulerService(
        scheduler_store,
        SyntheticRunner(),
        database_settings,
        SchedulerSettings(  # type: ignore[call-arg]
            enabled=True, jobs="bls,fed,treasury,funding,open_interest,coinbase", _env_file=None
        ),
        now=lambda: clock[0],
    )
    service.run(once=True)
    assert called == []
    clock[0] += timedelta(days=1)
    service.run(once=True)
    assert called == list(JobId)
    clock[0] -= timedelta(hours=1)
    service.run(once=True)
    assert called == list(JobId)


def test_status_reader_role_uses_read_only_snapshot(database_settings: DatabaseSettings) -> None:
    engine = create_db_engine(database_settings, DatabaseRole.READ)
    statements: list[str] = []

    def record(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        assert len(SchedulerStore(engine).status()["jobs"]) == 6  # type: ignore[arg-type]
        assert statements[0] == "SET TRANSACTION READ ONLY"
        assert all(
            statement.startswith(("SET TRANSACTION READ ONLY", "SELECT"))
            for statement in statements
        )
    finally:
        engine.dispose()


def test_migration_preserves_existing_catalogs_and_facts(connection: Connection) -> None:
    from market_intelligence.db.tables import candles, metadata
    from tests.integration.test_schema import candle_values

    connection.execute(candles.insert().values(**candle_values(connection)))
    domain = [
        table
        for name, table in metadata.tables.items()
        if name not in {"scheduler_job_state", "scheduled_job_runs"}
    ]
    before = {table.name: connection.execute(sa.select(table)).mappings().all() for table in domain}
    config = migration_config()
    config.attributes["connection"] = connection
    command.downgrade(config, "0004")
    command.upgrade(config, "head")
    assert {
        table.name: connection.execute(sa.select(table)).mappings().all() for table in domain
    } == before
    assert connection.execute(sa.select(sa.func.count()).select_from(states)).scalar_one() == 6
