import json
from typing import Any

import pytest

from market_intelligence import cli
from market_intelligence.scheduler import cli as scheduler_cli


def forbid_database(**kwargs: Any) -> None:
    pytest.fail("Preview/disabled/invalid configuration must not load database settings")


def test_preview_has_six_fixed_definitions_without_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(scheduler_cli, "DatabaseSettings", forbid_database)
    assert cli.main(["schedule", "preview"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert not payload["enabled"] and len(payload["jobs"]) == 6
    assert not any(job["selected"] for job in payload["jobs"])


@pytest.mark.parametrize(
    "enabled,jobs", [("false", "bls"), ("true", ""), ("true", "bad"), ("true", "bls,bls")]
)
def test_dispatch_guard_precedes_database(
    monkeypatch: pytest.MonkeyPatch, enabled: str, jobs: str, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCHEDULER_ENABLED", enabled)
    monkeypatch.setenv("SCHEDULER_JOBS", jobs)
    monkeypatch.setattr(scheduler_cli, "DatabaseSettings", forbid_database)
    assert cli.main(["schedule", "run", "--once"]) == 1
    assert json.loads(capsys.readouterr().out)["event"] == "scheduler_command_failed"


def test_status_limit_rejected_before_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_cli, "DatabaseSettings", forbid_database)
    assert cli.main(["schedule", "status", "--limit", "0"]) == 1


def test_unknown_control_job_and_bad_reconciliation_uuid_are_parser_errors() -> None:
    with pytest.raises(SystemExit):
        cli.main(["schedule", "resume", "arbitrary"])
    with pytest.raises(SystemExit):
        cli.main(["schedule", "resume", "open_interest", "--reconcile-run", "bad"])
