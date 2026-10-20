from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.db.fomc_store import FomcStore
from market_intelligence.fomc.artifacts import ArtifactStore
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.client import FomcAcquisitionClient
from market_intelligence.fomc.models import CALENDAR_URL
from market_intelligence.fomc.service import FomcIngestionError, ingest_pilot
from tests.fomc_fakes import calendar_body, document_body
from tests.integration.test_fomc_store import NOW, counts

pytest_plugins = ["tests.integration.test_fomc_store"]

pytestmark = pytest.mark.integration


class Clock:
    elapsed = 0.0

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds

    def now(self) -> datetime:
        return NOW + timedelta(seconds=self.elapsed)

    def monotonic(self) -> float:
        return self.elapsed


def run(store: FomcStore, root: Path, fault: str | None = None) -> Any:
    clock = Clock()
    urls: list[str] = []

    class CheckedArtifacts(ArtifactStore):
        def put(self, body: bytes) -> Path:
            assert store.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
            return super().put(body)

    def handler(request: httpx.Request) -> httpx.Response:
        assert store.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        assert store.status()[0]["status"] == "running"
        url = str(request.url)
        urls.append(url)
        if url == PILOT_DOCUMENTS[1].url:
            if fault == "interrupt":
                raise KeyboardInterrupt
            if fault == "reject":
                return httpx.Response(429)
            if fault == "unexpected":
                raise RuntimeError("Untrusted failure detail must remain private")
        if url == CALENDAR_URL:
            body = calendar_body()
        else:
            spec = next(d for d in PILOT_DOCUMENTS if d.url == url)
            body = document_body(spec)
            if fault == "invalid" and spec == PILOT_DOCUMENTS[1]:
                body = body.replace(b"September 17-18", b"September 16-18")
        return httpx.Response(
            200, headers={"Content-Type": "text/html; charset=utf-8"}, stream=httpx.ByteStream(body)
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = FomcAcquisitionClient(
            http, monotonic=clock.monotonic, sleep=clock.sleep, now=clock.now
        )
        return ingest_pilot(store, client, CheckedArtifacts(root), now=clock.now)


def test_complete_synthetic_pilot_releases_connections(store: FomcStore, tmp_path: Path) -> None:
    result = run(store, tmp_path)
    assert result.documents_completed == result.new_versions == 4
    assert result.http_attempts == 5
    assert result.wire_bytes == result.entity_bytes
    assert counts(store.engine) == (4, 5, 4)
    assert store.status()[0]["status"] == "succeeded"
    assert len(list(tmp_path.rglob("*.html"))) == 5


@pytest.mark.parametrize(
    "fault,code",
    [
        ("reject", "source_rejected"),
        ("invalid", "invalid_fomc_document"),
        ("interrupt", "interrupted"),
        ("unexpected", "internal_error"),
    ],
)
def test_later_failure_preserves_prior_documents_and_controlled_audit(
    store: FomcStore, tmp_path: Path, fault: str, code: str
) -> None:
    with pytest.raises(FomcIngestionError) as caught:
        run(store, tmp_path, fault)
    assert caught.value.code == code and caught.value.audit_recorded
    assert counts(store.engine) == (1, 2, 1)
    assert len(list(tmp_path.rglob("*.html"))) == 2
    audit = store.status()[0]
    assert audit["status"] == "failed" and audit["documents_completed"] == 1
    assert audit["http_attempts"] == 3


def test_database_failure_reports_possible_unreferenced_artifact(
    store: FomcStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> bool:
        raise SQLAlchemyError("sensitive connection details must not escape")

    monkeypatch.setattr(store, "record_document", fail)
    with pytest.raises(FomcIngestionError) as caught:
        run(store, tmp_path)
    assert str(caught.value) == "database_error"
    assert caught.value.unreferenced_artifact_possible and caught.value.audit_recorded
    assert counts(store.engine) == (0, 1, 0)
    assert len(list(tmp_path.rglob("*.html"))) == 2


def test_running_reservation_prevents_any_second_acquisition(
    store: FomcStore, tmp_path: Path
) -> None:
    store.reserve(NOW)
    with pytest.raises(FomcIngestionError) as caught:
        run(store, tmp_path)
    assert caught.value.code == "running_command" and caught.value.run_id is None
    assert list(tmp_path.iterdir()) == []
