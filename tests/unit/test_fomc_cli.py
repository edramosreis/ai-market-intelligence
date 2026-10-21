import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from market_intelligence.cli import main
from market_intelligence.fomc import cli


@pytest.mark.parametrize(
    "args", [[], ["--pilot", "--status"], ["--reconcile-run", "bad"], ["--url", "https://invalid"]]
)
def test_invalid_options_fail_before_configuration(
    args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = MagicMock(side_effect=AssertionError("Configuration must not load"))
    monkeypatch.setattr(cli, "DatabaseSettings", mock)
    with pytest.raises(SystemExit):
        main(["ingest-fomc", *args])
    mock.assert_not_called()


def test_demo_environment_rejected_before_database_or_http(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in dict(
        POSTGRES_DB="market_intelligence",
        POSTGRES_HOST="db",
        POSTGRES_PORT="5432",
        POSTGRES_INGEST_USER="ingest",
        POSTGRES_INGEST_PASSWORD="synthetic-test-only",
    ).items():
        monkeypatch.setenv(key, value)
    engine = MagicMock(side_effect=AssertionError("Database must not connect"))
    http = MagicMock(side_effect=AssertionError("HTTP must not open"))
    monkeypatch.setattr(cli, "create_db_engine", engine)
    monkeypatch.setattr("market_intelligence.fomc.cli.httpx.Client", http)
    assert main(["ingest-fomc", "--pilot"]) == 1
    value = json.loads(capsys.readouterr().out)
    assert value["code"] == "configuration_or_database_error"
    engine.assert_not_called()
    http.assert_not_called()


def test_status_uses_reader_and_has_no_http_or_artifact_access(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, value in dict(
        POSTGRES_DB="market_intelligence_m3",
        POSTGRES_HOST="db",
        POSTGRES_PORT="5432",
        POSTGRES_READ_USER="m3_reader",
        POSTGRES_READ_PASSWORD="synthetic-test-only",
    ).items():
        monkeypatch.setenv(key, value)
    engine = MagicMock()
    monkeypatch.setattr(cli, "create_db_engine", lambda settings, role: engine)
    store = MagicMock()
    store.status.return_value = []
    monkeypatch.setattr(cli, "FomcStore", lambda _: store)
    monkeypatch.setattr(
        "market_intelligence.fomc.cli.httpx.Client",
        MagicMock(side_effect=AssertionError("No HTTP")),
    )
    monkeypatch.setattr(cli, "ArtifactStore", MagicMock(side_effect=AssertionError("No artifacts")))
    assert main(["ingest-fomc", "--status"]) == 0
    assert json.loads(capsys.readouterr().out)["runs"] == []
    store.reserve.assert_not_called()
    engine.dispose.assert_called_once()


def test_status_refuses_unused_artifact_override() -> None:
    parser = argparse.ArgumentParser()
    cli.add_arguments(parser)
    args = parser.parse_args(["--status", "--artifact-root", "unused"])
    assert args.artifact_root == Path("unused")
    assert cli.execute(args) == 1


def test_host_artifacts_stay_in_linked_checkout_ignored_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    expected = tmp_path / "data/fomc"
    with pytest.raises(ValueError):
        cli.private_artifact_root(expected)
    (tmp_path / ".git").write_text("gitdir: synthetic-linked-worktree")
    assert cli.private_artifact_root(expected) == expected
    with pytest.raises(ValueError):
        cli.private_artifact_root(tmp_path.parent / "demo/data/fomc")
    with pytest.raises(ValueError):
        cli.private_artifact_root(tmp_path / "src/originals")


@pytest.mark.parametrize("budget", ["0", "9", "-1", "invalid"])
def test_request_budget_rejected_before_configuration(
    budget: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = MagicMock(side_effect=AssertionError("No configuration"))
    monkeypatch.setattr(cli, "DatabaseSettings", configuration)
    with pytest.raises(SystemExit):
        main(["ingest-fomc", "--pilot", "--max-requests", budget])
    configuration.assert_not_called()


def test_request_budget_reaches_actual_acquisition_client(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in dict(
        POSTGRES_DB="market_intelligence_m3",
        POSTGRES_HOST="db",
        POSTGRES_PORT="5432",
        POSTGRES_INGEST_USER="m3_ingest",
        POSTGRES_INGEST_PASSWORD="synthetic-test-only",
    ).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(cli, "create_db_engine", MagicMock())
    monkeypatch.setattr(cli, "FomcStore", MagicMock())
    monkeypatch.setattr(cli, "private_artifact_root", lambda value: value)
    acquisition = MagicMock()
    monkeypatch.setattr(cli, "FomcAcquisitionClient", acquisition)

    class StopAfterClient(Exception):
        pass

    monkeypatch.setattr(cli, "ingest_pilot", MagicMock(side_effect=StopAfterClient))
    with pytest.raises(StopAfterClient):
        main(["ingest-fomc", "--pilot", "--max-requests", "5"])
    assert acquisition.call_args.kwargs["max_requests"] == 5
