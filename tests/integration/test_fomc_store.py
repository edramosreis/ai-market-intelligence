from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.fomc_store import FomcStore, FomcStoreError
from market_intelligence.db.fomc_tables import (
    fomc_current as current,
)
from market_intelligence.db.fomc_tables import (
    fomc_fetch_receipts as receipts,
)
from market_intelligence.db.fomc_tables import (
    fomc_raw_versions as versions,
)
from market_intelligence.fomc.artifacts import ArtifactStore
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.client import FetchedCalendar
from market_intelligence.fomc.validation import validate_calendar, validate_document
from tests.fomc_fakes import calendar_body, document_body, fetched

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 20, tzinfo=UTC)
STATS = dict(http_attempts=5, wire_bytes=10000, entity_bytes=10000)


@pytest.fixture
def store(database_settings: DatabaseSettings, admin_engine: Engine) -> Iterator[FomcStore]:
    engine = create_db_engine(database_settings, DatabaseRole.INGEST)
    try:
        yield FomcStore(engine)
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(
                sa.text(
                    "TRUNCATE fomc_current, fomc_fetch_receipts, "
                    "fomc_raw_versions, fomc_ingestion_runs"
                )
            )


def start(
    store: FomcStore, artifacts: ArtifactStore, at: datetime = NOW
) -> tuple[Any, FetchedCalendar]:
    run = store.reserve(at)
    body = calendar_body()
    calendar = FetchedCalendar(
        body, uuid4(), at, at, "text/html", "identity", len(body), None, None
    )
    artifacts.put(body)
    store.record_calendar(run, calendar, validate_calendar(body), artifacts, STATS)
    return run, calendar


def load(store: FomcStore, artifacts: ArtifactStore, at: datetime) -> Any:
    run, calendar = start(store, artifacts, at)
    for spec in PILOT_DOCUMENTS:
        raw = fetched(spec, at=at)
        artifacts.put(raw.body)
        store.record_document(
            run,
            validate_document(raw, validate_calendar(calendar.body)),
            calendar.receipt_id,
            artifacts,
            STATS,
        )
    store.finish(run, at, STATS)
    return run


def counts(engine: Engine) -> tuple[int, int, int]:
    with engine.connect() as conn:
        return tuple(
            conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
            for table in [versions, receipts, current]
        )  # type: ignore[return-value]


def test_replay_keeps_first_receipt_and_appends_confirmations(
    store: FomcStore, tmp_path: Path
) -> None:
    artifacts = ArtifactStore(tmp_path)
    load(store, artifacts, NOW)
    with store.engine.connect() as conn:
        before = conn.execute(sa.select(versions)).mappings().all()
        old = conn.execute(sa.select(current)).mappings().all()
    load(store, artifacts, NOW + timedelta(days=1))
    assert counts(store.engine) == (4, 10, 4)
    with store.engine.connect() as conn:
        assert conn.execute(sa.select(versions)).mappings().all() == before
        after = conn.execute(sa.select(current)).mappings().all()
        assert {r["last_receipt_id"] for r in old}.isdisjoint({r["last_receipt_id"] for r in after})


def test_changed_bytes_create_version_without_replacing_first(
    store: FomcStore, tmp_path: Path
) -> None:
    artifacts = ArtifactStore(tmp_path)
    load(store, artifacts, NOW)
    at = NOW + timedelta(days=1)
    run, calendar = start(store, artifacts, at)
    spec = PILOT_DOCUMENTS[0]
    raw = fetched(spec, document_body(spec, "<p>Synthetic changed policy content.</p>"), at=at)
    artifacts.put(raw.body)
    assert store.record_document(
        run,
        validate_document(raw, validate_calendar(calendar.body)),
        calendar.receipt_id,
        artifacts,
        STATS,
    )
    store.finish(run, at, STATS, "interrupted")
    assert counts(store.engine) == (5, 7, 4)
    with store.engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(current.c.raw_version_id).where(current.c.document_id == spec.document_id)
            ).scalar_one()
            == raw.raw_version_id
        )


@pytest.mark.parametrize("equal_time", [False, True])
def test_older_and_ambiguous_equal_time_receipts_rejected(
    store: FomcStore, tmp_path: Path, equal_time: bool
) -> None:
    artifacts = ArtifactStore(tmp_path)
    load(store, artifacts, NOW)
    run, calendar = start(store, artifacts, NOW + timedelta(days=1))
    spec = PILOT_DOCUMENTS[0]
    raw = fetched(
        spec,
        document_body(spec, "<p>Changed.</p>"),
        at=NOW if equal_time else NOW - timedelta(seconds=1),
    )
    artifacts.put(raw.body)
    with pytest.raises(FomcStoreError, match="stale_receipt"):
        store.record_document(
            run,
            validate_document(raw, validate_calendar(calendar.body)),
            calendar.receipt_id,
            artifacts,
            STATS,
        )
    assert counts(store.engine) == (4, 6, 4)


def test_receipt_failure_rolls_back_version_current_and_count(
    store: FomcStore, tmp_path: Path
) -> None:
    artifacts = ArtifactStore(tmp_path)
    run, calendar = start(store, artifacts)
    raw = fetched(PILOT_DOCUMENTS[0])
    # Calendar receipt already owns this UUID, causing failure after version insertion.
    from dataclasses import replace

    raw = replace(raw, receipt_id=calendar.receipt_id)
    artifacts.put(raw.body)
    with pytest.raises(IntegrityError):
        store.record_document(
            run,
            validate_document(raw, validate_calendar(calendar.body)),
            calendar.receipt_id,
            artifacts,
            STATS,
        )
    assert counts(store.engine) == (0, 1, 0)
    assert store.status()[0]["documents_completed"] == 0


def test_reservation_concurrency_and_explicit_crash_reconciliation(store: FomcStore) -> None:
    def reserve(_: int) -> Any:
        try:
            return store.reserve(NOW)
        except FomcStoreError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, [1, 2]))
    assert outcomes.count("running_command") == 1
    run = next(value for value in outcomes if value != "running_command")
    assert store.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    with pytest.raises(FomcStoreError, match="running_command"):
        store.reserve(NOW + timedelta(days=10))
    store.reconcile(run, NOW + timedelta(days=10))
    assert store.status()[0]["error_code"] == "interrupted"
    store.reserve(NOW + timedelta(days=11))


def test_composite_fks_reject_transplanted_first_and_current_receipts(
    store: FomcStore, tmp_path: Path, admin_engine: Engine
) -> None:
    load(store, ArtifactStore(tmp_path), NOW)
    with admin_engine.connect() as conn, conn.begin():
        rows = conn.execute(sa.select(current)).mappings().all()
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                current.update()
                .where(current.c.document_id == rows[0]["document_id"])
                .values(last_receipt_id=rows[1]["last_receipt_id"])
            )
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                versions.update()
                .where(versions.c.document_id == rows[0]["document_id"])
                .values(first_receipt_id=rows[1]["last_receipt_id"])
            )
            conn.execute(sa.text("SET CONSTRAINTS fk_fomc_first_receipt IMMEDIATE"))


def test_status_is_reader_only_snapshot(
    store: FomcStore, database_settings: DatabaseSettings
) -> None:
    run = store.reserve(NOW)
    engine = create_db_engine(database_settings, DatabaseRole.READ)
    statements: list[str] = []

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        assert FomcStore(engine).status()[0]["run_id"] == run
        assert statements[0] == "SET TRANSACTION READ ONLY"
        assert all(s.startswith(("SET TRANSACTION READ ONLY", "SELECT")) for s in statements)
    finally:
        engine.dispose()
