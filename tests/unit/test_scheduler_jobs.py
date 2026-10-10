from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from market_intelligence.scheduler.jobs import (
    DEFINITIONS,
    JOBS,
    JobId,
    SchedulerSettings,
    due_slot,
    latest_slot,
    next_slot,
)

NOW = datetime(2026, 10, 10, 22, tzinfo=UTC)


@pytest.mark.parametrize(
    "job,hour,minute",
    [
        (JobId.COINBASE, 21, 56),
        (JobId.OPEN_INTEREST, 22, 0),
        (JobId.FUNDING, 21, 5),
        (JobId.TREASURY, 22, 0),
        (JobId.FED, 22, 30),
        (JobId.BLS, 23, 0),
    ],
)
def test_slots_use_utc_boundaries(job: JobId, hour: int, minute: int) -> None:
    reference = NOW if job not in {JobId.FED, JobId.BLS} else NOW + timedelta(hours=1)
    assert latest_slot(JOBS[job], reference) == reference.replace(hour=hour, minute=minute)
    assert latest_slot(
        JOBS[job], reference.astimezone(timezone(timedelta(hours=1)))
    ) == latest_slot(JOBS[job], reference)


def test_first_enrollment_and_exact_boundary_wait_for_future() -> None:
    job = JOBS[JobId.OPEN_INTEREST]
    assert due_slot(job, NOW, NOW) is None
    assert next_slot(job, NOW) == NOW + timedelta(minutes=15)
    due = due_slot(job, NOW + timedelta(seconds=1), NOW + timedelta(minutes=15))
    assert due is not None and due.skipped_slots == 0


def test_downtime_coalesces_and_rollback_does_not_repeat() -> None:
    job = JOBS[JobId.COINBASE]
    due = due_slot(job, NOW, NOW + timedelta(hours=12))
    assert due is not None and due.slot == NOW + timedelta(hours=11, minutes=56)
    assert due.skipped_slots == 143
    assert due_slot(job, due.slot, NOW + timedelta(hours=11)) is None
    assert due_slot(job, due.slot, due.slot) is None


def test_bls_cooldown_delays_due_slot_without_losing_it() -> None:
    job = JOBS[JobId.BLS]
    cursor = NOW + timedelta(hours=1)
    allowed = cursor + timedelta(days=1, minutes=2)
    assert due_slot(job, cursor, allowed - timedelta(seconds=1), allowed) is None
    due = due_slot(job, cursor, allowed, allowed)
    assert due is not None and due.slot == cursor + timedelta(days=1)


@pytest.mark.parametrize("value", ["unknown", "bls,bls", "bls,", ",", "coinbase;anything"])
def test_job_selection_is_fixed_and_unique(value: str) -> None:
    with pytest.raises(ValidationError):
        SchedulerSettings(jobs=value, _env_file=None)  # type: ignore[call-arg]


def test_disabled_defaults_and_definition_fingerprints() -> None:
    settings = SchedulerSettings(_env_file=None)  # type: ignore[call-arg]
    assert not settings.enabled and settings.selected == ()
    selected = SchedulerSettings(jobs=" bls, fed ", _env_file=None)  # type: ignore[call-arg]
    assert selected.selected == (JobId.BLS, JobId.FED)
    assert len({job.fingerprint for job in DEFINITIONS}) == 6
    assert all(
        len(job.fingerprint) == 64 and "--max-seconds" in job.arguments for job in DEFINITIONS
    )


def test_naive_clock_is_rejected() -> None:
    with pytest.raises(ValueError):
        next_slot(JOBS[JobId.BLS], datetime(2026, 10, 10))
