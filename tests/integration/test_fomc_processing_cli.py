import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from market_intelligence.cli import main
from market_intelligence.db.fomc_store import FomcStore
from market_intelligence.fomc import processing_cli
from market_intelligence.fomc.artifacts import ArtifactStore
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.validation import validate_calendar, validate_document
from tests.fomc_fakes import fetched
from tests.fomc_passage_fakes import passage_body
from tests.integration.test_fomc_processing import FIRST, SECOND, collect_one
from tests.integration.test_fomc_store import NOW, STATS, start, store  # noqa: F401

pytestmark = pytest.mark.integration


def test_manual_processing_and_reader_status_use_stored_artifacts_and_safe_output(
    store: FomcStore,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_version = collect_one(store, ArtifactStore(tmp_path))
    monkeypatch.setattr(processing_cli, "private_artifact_root", lambda _: tmp_path)
    monkeypatch.setattr("httpx.Client", MagicMock(side_effect=AssertionError("No HTTP required")))
    assert main(["process-fomc", "--pilot", "--artifact-root", str(tmp_path)]) == 1
    printed = capsys.readouterr().out
    pilot = json.loads(printed)
    assert pilot["event"] == "fomc_processing_completed"
    assert len(pilot["documents"]) == 4
    assert pilot["documents"][0]["outcome"] == "created"
    assert all(row["outcome"] == "not_collected" for row in pilot["documents"][1:])
    assert FIRST not in printed and SECOND not in printed
    assert main(["process-fomc", "--status"]) == 0
    printed = capsys.readouterr().out
    status = json.loads(printed)
    assert status["documents"][0]["state"] == "ready"
    assert status["documents"][0]["raw_version_id"] == raw_version
    assert status["documents"][0]["passage_count"] == 1
    assert status["documents"][0]["passage_set_id"] == pilot["documents"][0]["passage_set_id"]
    assert FIRST not in printed and SECOND not in printed


def test_complete_cli_pilot_and_replay(
    store: FomcStore,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifacts = ArtifactStore(tmp_path)
    run, calendar = start(store, artifacts, NOW)
    evidence = validate_calendar(calendar.body)
    for spec in PILOT_DOCUMENTS:
        raw = fetched(spec, passage_body(spec, f"<p>{FIRST}</p><p>{SECOND}</p>"), at=NOW)
        artifacts.put(raw.body)
        store.record_document(
            run, validate_document(raw, evidence), calendar.receipt_id, artifacts, STATS
        )
    store.finish(run, NOW, STATS)
    monkeypatch.setattr(processing_cli, "private_artifact_root", lambda _: tmp_path)
    recorded_ids: list[list[str]] = []
    for expected in ("created", "reused"):
        assert main(["process-fomc", "--pilot"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert len(result["documents"]) == 4
        assert all(row["outcome"] == expected for row in result["documents"])
        assert sum(row["passage_count"] for row in result["documents"]) == 4
        recorded_ids.append([row["passage_set_id"] for row in result["documents"]])
    assert recorded_ids[0] == recorded_ids[1]
