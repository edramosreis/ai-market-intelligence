import json
import sys
import time
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr

from market_intelligence.config import DatabaseSettings
from market_intelligence.scheduler.jobs import JobId
from market_intelligence.scheduler.models import OutcomeCode
from market_intelligence.scheduler.process import (
    EventCollector,
    ProcessRunner,
    minimal_environment,
    require_runtime,
)


def emit(collector: EventCollector, row: dict[str, Any], *, stderr: bool = False) -> None:
    collector.line(json.dumps(row).encode(), stderr=stderr)


@pytest.mark.parametrize(
    "job,progress,completion",
    [
        (JobId.COINBASE, "chunk_succeeded", "ingestion_completed"),
        (JobId.TREASURY, "treasury_month_succeeded", "treasury_ingestion_completed"),
        (JobId.FUNDING, "funding_window_succeeded", "funding_ingestion_completed"),
        (JobId.BLS, "macro_window_succeeded", "macro_ingestion_completed"),
        (JobId.FED, "macro_window_succeeded", "macro_ingestion_completed"),
    ],
)
def test_native_success_events_retain_only_audits(
    job: JobId, progress: str, completion: str
) -> None:
    collector = EventCollector(job)
    provider = (
        {"provider": "bls" if job == JobId.BLS else "federal_reserve_board"}
        if job in {JobId.BLS, JobId.FED}
        else {}
    )
    audit = uuid4()
    emit(collector, {"event": progress, "run_id": str(audit), "received": 24, **provider})
    collector.line(b"A bounded library warning", stderr=True)
    emit(collector, {"event": completion, **provider})
    result = collector.outcome(0)
    assert result.status == "succeeded" and result.audits[0].id == audit
    assert not hasattr(collector, "raw_output")


def test_oi_receipt_not_a_scheduled_historical_observation() -> None:
    collector = EventCollector(JobId.OPEN_INTEREST)
    emit(
        collector,
        {
            "event": "open_interest_collected",
            "run_id": str(uuid4()),
            "snapshot_id": str(uuid4()),
            "source_event_time": None,
        },
    )
    assert collector.outcome(0).status == "succeeded"


@pytest.mark.parametrize(
    "code,status,pause",
    [
        ("source_rejected", "failed", True),
        ("invalid_payload", "failed", True),
        ("http_error", "failed", False),
        ("retry_exhausted", "failed", False),
        ("concurrent_job", "failed", False),
        ("interrupted", "uncertain", True),
        ("internal_error", "uncertain", True),
        ("database_error", "uncertain", True),
    ],
)
def test_failure_policy_preserves_audited_outcomes(code: str, status: str, pause: bool) -> None:
    collector = EventCollector(JobId.BLS)
    emit(
        collector,
        {
            "event": "macro_ingestion_failed",
            "provider": "bls",
            "code": code,
            "audit_recorded": True,
            "run_id": str(uuid4()),
        },
        stderr=True,
    )
    outcome = collector.outcome(1)
    assert (outcome.status, outcome.pause) == (status, pause)


def test_unrecorded_attempt_empty_output_and_exit_mismatch_are_uncertain() -> None:
    collector = EventCollector(JobId.BLS)
    assert collector.outcome(0).status == "uncertain"
    emit(
        collector,
        {
            "event": "macro_ingestion_failed",
            "provider": "bls",
            "code": "http_error",
            "audit_recorded": False,
            "run_id": str(uuid4()),
        },
        stderr=True,
    )
    assert collector.outcome(1).status == "uncertain"
    assert collector.outcome(0).status == "uncertain"


@pytest.mark.parametrize(
    "payload,stderr",
    [
        (b'{"event":"ingestion_completed","event":"ingestion_completed"}', False),
        (b'{"event":"unknown"}', False),
        (b"[]", False),
        (b"not json", False),
        (b"\xff", False),
        (b"x" * 8193, True),
        (b'{"event":"ingestion_completed","chunks":true}', False),
        (b'{"event":"ingestion_completed","received":-1}', False),
        (b'{"event":"chunk_succeeded","run_id":"invalid"}', False),
        (b'{"event":"ingestion_completed"}', True),
    ],
)
def test_invalid_output_is_rejected(payload: bytes, stderr: bool) -> None:
    with pytest.raises((ValueError, UnicodeError)):
        EventCollector(JobId.COINBASE).line(payload, stderr=stderr)


def test_transplanted_provider_and_duplicate_audit_are_rejected() -> None:
    collector = EventCollector(JobId.BLS)
    with pytest.raises(ValueError):
        emit(
            collector,
            {
                "event": "macro_window_succeeded",
                "provider": "federal_reserve_board",
                "run_id": str(uuid4()),
            },
        )
    row = {"event": "macro_window_succeeded", "provider": "bls", "run_id": str(uuid4())}
    emit(collector, row)
    with pytest.raises(ValueError):
        emit(collector, row)
    with pytest.raises(ValueError):
        emit(
            collector, {"event": "macro_ingestion_completed", "provider": "bls", "requests_used": 4}
        )


def test_child_environment_drops_all_unrelated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unused")
    monkeypatch.setenv("POSTGRES_ADMIN_PASSWORD", "synthetic-unused")
    monkeypatch.setenv("PYTHONPATH", "untrusted")
    settings = DatabaseSettings(  # type: ignore[call-arg]
        db="synthetic",
        ingest_user="writer",
        ingest_password=SecretStr("synthetic-writer"),
        _env_file=None,
    )
    environment = minimal_environment(settings)
    assert set(environment) == {
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_INGEST_USER",
        "POSTGRES_INGEST_PASSWORD",
        "PYTHONUNBUFFERED",
        "LANG",
    }
    with pytest.raises(ValueError):
        require_runtime(settings)


@pytest.mark.skipif(sys.platform != "linux", reason="Active supervision targets Linux containers")
@pytest.mark.parametrize(
    "behavior,code",
    [
        ("time.sleep(10)", OutcomeCode.TIMEOUT),
        ("print('not json', flush=True); time.sleep(10)", OutcomeCode.OUTPUT_INVALID),
        (
            "sys.stderr.write(('warning\\n'*10000)); sys.stderr.flush(); time.sleep(10)",
            OutcomeCode.OUTPUT_LIMIT,
        ),
        ("os.close(1); os.close(2); time.sleep(10)", OutcomeCode.TIMEOUT),
    ],
)
def test_real_synthetic_children_are_bounded_and_reaped(behavior: str, code: OutcomeCode) -> None:
    start = time.monotonic()
    result = ProcessRunner()._supervise(
        (sys.executable, "-c", "import time,sys,os; " + behavior),
        {},
        JobId.OPEN_INTEREST,
        0.3,
        lambda: True,
        lambda: False,
        0.2,
    )
    assert result.status == "uncertain" and result.code == code
    assert time.monotonic() - start < 2


@pytest.mark.skipif(sys.platform != "linux", reason="Active supervision targets Linux containers")
@pytest.mark.parametrize(
    "healthy,stopped,code",
    [(False, False, OutcomeCode.OWNERSHIP_LOST), (True, True, OutcomeCode.STOPPED)],
)
def test_stop_and_ownership_loss_interrupt_children(
    healthy: bool, stopped: bool, code: OutcomeCode
) -> None:
    result = ProcessRunner()._supervise(
        (sys.executable, "-c", "import time; time.sleep(10)"),
        {},
        JobId.OPEN_INTEREST,
        10,
        lambda: healthy,
        lambda: stopped,
        0.2,
    )
    assert result.code == code and result.pause


@pytest.mark.skipif(sys.platform != "linux", reason="Active supervision targets Linux containers")
def test_launch_failure_and_successful_synthetic_oi_child() -> None:
    runner = ProcessRunner()
    assert (
        runner._supervise(
            ("/missing-executable",), {}, JobId.OPEN_INTEREST, 1, lambda: True, lambda: False
        ).code
        == OutcomeCode.LAUNCH_FAILED
    )
    row = {"event": "open_interest_collected", "run_id": str(uuid4()), "snapshot_id": str(uuid4())}
    program = "import json; print(" + repr(json.dumps(row)) + ", flush=True)"
    result = runner._supervise(
        (sys.executable, "-c", program), {}, JobId.OPEN_INTEREST, 1, lambda: True, lambda: False
    )
    assert result.status == "succeeded" and len(result.audits) == 1
