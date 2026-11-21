import json
from unittest.mock import MagicMock

import pytest

from market_intelligence.cli import main
from market_intelligence.config import DatabaseRole
from market_intelligence.fomc import processing_cli


def test_processing_refuses_demo_target_before_opening_resources(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in {
        "POSTGRES_DB": "market_intelligence",
        "POSTGRES_HOST": "db",
        "POSTGRES_PORT": "5432",
        "POSTGRES_INGEST_USER": "ingest",
        "POSTGRES_INGEST_PASSWORD": "synthetic-test-only",
    }.items():
        monkeypatch.setenv(name, value)
    engine = MagicMock(side_effect=AssertionError("Unexpected database access"))
    artifacts = MagicMock(side_effect=AssertionError("Unexpected filesystem access"))
    monkeypatch.setattr(processing_cli, "create_db_engine", engine)
    monkeypatch.setattr(processing_cli, "private_artifact_root", artifacts)
    assert main(["process-fomc", "--pilot"]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "configuration_or_database_error"
    engine.assert_not_called()
    artifacts.assert_not_called()


def test_status_uses_only_the_reader_role_and_rejects_an_artifact_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in {
        "POSTGRES_DB": "market_intelligence_m3",
        "POSTGRES_HOST": "db",
        "POSTGRES_PORT": "5432",
        "POSTGRES_READ_USER": "m3_reader",
        "POSTGRES_READ_PASSWORD": "synthetic-test-only",
    }.items():
        monkeypatch.setenv(name, value)
    engine = MagicMock()
    factory = MagicMock(return_value=engine)
    store = MagicMock()
    store.status.return_value = []
    monkeypatch.setattr(processing_cli, "create_db_engine", factory)
    monkeypatch.setattr(processing_cli, "FomcProcessingStore", lambda _: store)
    monkeypatch.setattr(
        processing_cli, "ArtifactStore", MagicMock(side_effect=AssertionError("No artifact access"))
    )
    assert main(["process-fomc", "--status", "--artifact-root", "unused"]) == 1
    factory.assert_not_called()
    capsys.readouterr()
    assert main(["process-fomc", "--status"]) == 0
    assert factory.call_args.args[1] is DatabaseRole.READ
    assert json.loads(capsys.readouterr().out)["documents"] == []
    engine.dispose.assert_called_once()
