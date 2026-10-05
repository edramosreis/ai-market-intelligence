"""Real PostgreSQL ingestion transactions with a fake Coinbase HTTP transport."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.candle_store import CandleStore
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.tables import candles, ingestion_runs
from market_intelligence.ingestion.coinbase import BASE_URL, CoinbaseClient
from market_intelligence.ingestion.models import (
    ErrorCode,
    IngestionError,
    TimeWindow,
    parse_instant,
)
from market_intelligence.ingestion.service import ingest

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 5, tzinfo=UTC)
START = parse_instant("2024-01-01")
WINDOW = TimeWindow(START, START + timedelta(minutes=15))
MONTH_BOUNDARY = parse_instant("2024-02-01")


@pytest.fixture
def store(database_settings: DatabaseSettings, admin_engine: Engine) -> Iterator[CandleStore]:
    engine = create_db_engine(database_settings, DatabaseRole.INGEST)
    try:
        yield CandleStore(engine)
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(candles.delete())
            conn.execute(ingestion_runs.delete())


def wire_rows(request: httpx.Request) -> list[list[int]]:
    start = parse_instant(request.url.params["start"])
    end = parse_instant(request.url.params["end"])
    return [
        [int((start + timedelta(minutes=5 * i)).timestamp()), 100, 120, 110, 115, 1]
        for i in range((end - start) // timedelta(minutes=5))
    ]


@contextmanager
def source_with(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    **options: Any,
) -> Iterator[CoinbaseClient]:
    def dispatch(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/products/BTC-USD":
            return httpx.Response(
                200,
                json={
                    "id": "BTC-USD",
                    "base_currency": "BTC",
                    "quote_currency": "USD",
                    "margin_enabled": False,
                },
            )
        return handler(request) if handler else httpx.Response(200, json=wire_rows(request))

    with httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(dispatch)) as http:
        yield CoinbaseClient(http, request_interval=0, attempts=1, **options)


def snapshot(admin_engine: Engine) -> list[dict[str, Any]]:
    with admin_engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(sa.select(candles).order_by(candles.c.opened_at)).mappings()
        ]


def runs(admin_engine: Engine) -> list[dict[str, Any]]:
    with admin_engine.connect() as conn:
        return [dict(row) for row in conn.execute(sa.select(ingestion_runs)).mappings()]


def test_replay_preserves_candle_count_and_unchanged_provenance(
    store: CandleStore, admin_engine: Engine
) -> None:
    with source_with() as source:
        first = ingest(source, store, WINDOW, now=lambda: NOW)[0]
    before = snapshot(admin_engine)
    with source_with() as source:
        replay = ingest(source, store, WINDOW, now=lambda: NOW + timedelta(hours=1))[0]
    assert first.inserted == 3 and first.missing_buckets == 0
    assert (replay.inserted, replay.updated, replay.unchanged) == (0, 0, 3)
    assert snapshot(admin_engine) == before
    assert len(runs(admin_engine)) == 2


def test_correction_changes_only_affected_values_and_latest_provenance(
    store: CandleStore, admin_engine: Engine
) -> None:
    with source_with() as source:
        ingest(source, store, WINDOW, now=lambda: NOW)
    before = snapshot(admin_engine)

    def corrected(request: httpx.Request) -> httpx.Response:
        rows = wire_rows(request)
        rows[1][4] = 116
        return httpx.Response(200, json=rows)

    with source_with(corrected) as source:
        report = ingest(source, store, WINDOW, now=lambda: NOW + timedelta(hours=1))[0]
    after = snapshot(admin_engine)
    assert (report.inserted, report.updated, report.unchanged) == (0, 1, 2)
    assert after[0] == before[0] and after[2] == before[2]
    assert after[1]["close"] == Decimal(116)
    assert after[1]["first_ingested_at"] == before[1]["first_ingested_at"]
    assert after[1]["last_updated_at"] == NOW + timedelta(hours=1)
    assert after[1]["last_ingestion_run_id"] == report.run_id


def test_missing_buckets_are_reported_and_resume_does_not_claim_complete_coverage(
    store: CandleStore, admin_engine: Engine
) -> None:
    def gapped(request: httpx.Request) -> httpx.Response:
        rows = wire_rows(request)
        return httpx.Response(200, json=[rows[0], rows[2], rows[0]])

    with source_with(gapped) as source:
        report = ingest(source, store, WINDOW, now=lambda: NOW)[0]
    assert (report.received, report.missing_buckets) == (2, 1)
    assert len(snapshot(admin_engine)) == 2
    with source_with(
        lambda request: pytest.fail("Resume must not fetch a completed window")
    ) as source:
        report = ingest(source, store, WINDOW, now=lambda: NOW, resume=True)[0]
    assert report.skipped and report.missing_buckets == 1
    with source_with() as source:
        repaired = ingest(source, store, WINDOW, now=lambda: NOW)[0]
    assert repaired.inserted == 1 and repaired.unchanged == 2 and repaired.missing_buckets == 0


def test_failed_later_month_preserves_first_month_and_resume_retries_only_unfinished_chunk(
    store: CandleStore, admin_engine: Engine
) -> None:
    window = TimeWindow(
        MONTH_BOUNDARY - timedelta(minutes=5), MONTH_BOUNDARY + timedelta(minutes=10)
    )

    def fail_second(request: httpx.Request) -> httpx.Response:
        if parse_instant(request.url.params["start"]) >= MONTH_BOUNDARY:
            return httpx.Response(400)
        return httpx.Response(200, json=wire_rows(request))

    with source_with(fail_second) as source, pytest.raises(IngestionError) as error:
        ingest(source, store, window, now=lambda: NOW)
    assert error.value.code == ErrorCode.HTTP_ERROR and error.value.audit_recorded
    assert len(snapshot(admin_engine)) == 1
    assert {run["status"] for run in runs(admin_engine)} == {"succeeded", "failed"}
    fetched = []

    def retry(request: httpx.Request) -> httpx.Response:
        fetched.append(parse_instant(request.url.params["start"]))
        return httpx.Response(200, json=wire_rows(request))

    with source_with(retry) as source:
        reports = ingest(source, store, window, now=lambda: NOW, resume=True)
    assert reports[0].skipped and reports[1].inserted == 2
    assert fetched == [MONTH_BOUNDARY]
    assert len(snapshot(admin_engine)) == 3 and len(runs(admin_engine)) == 3


def test_validation_failure_after_a_valid_page_leaves_entire_chunk_empty(
    store: CandleStore, admin_engine: Engine
) -> None:
    window = TimeWindow(START, START + timedelta(minutes=5 * 251))

    def invalid_second(request: httpx.Request) -> httpx.Response:
        rows = wire_rows(request)
        if parse_instant(request.url.params["start"]) > START:
            rows[0][3] = 0
        return httpx.Response(200, json=rows)

    with source_with(invalid_second) as source, pytest.raises(IngestionError) as error:
        ingest(source, store, window, now=lambda: NOW)
    assert error.value.code == ErrorCode.INVALID_CANDLE
    assert not snapshot(admin_engine)
    assert runs(admin_engine)[0]["status"] == "failed"


def test_success_audit_update_and_multiple_candle_batches_roll_back_together(
    store: CandleStore, admin_engine: Engine
) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            sa.text("""
            CREATE FUNCTION test_reject_success() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.status = 'succeeded' THEN RAISE EXCEPTION 'synthetic failure'; END IF;
                RETURN NEW;
            END $$
        """)
        )
        conn.execute(
            sa.text("""
            CREATE TRIGGER test_reject_success BEFORE UPDATE ON ingestion_runs
            FOR EACH ROW EXECUTE FUNCTION test_reject_success()
        """)
        )
    try:
        window = TimeWindow(START, START + timedelta(minutes=5 * 501))
        with source_with() as source, pytest.raises(IngestionError) as error:
            ingest(source, store, window, now=lambda: NOW)
        assert error.value.code == ErrorCode.DATABASE_ERROR and error.value.audit_recorded
        assert not snapshot(admin_engine)
        run = runs(admin_engine)[0]
        assert run["status"] == "failed" and run["inserted"] == 0
    finally:
        with admin_engine.begin() as conn:
            conn.execute(sa.text("DROP TRIGGER test_reject_success ON ingestion_runs"))
            conn.execute(sa.text("DROP FUNCTION test_reject_success()"))


def test_session_lock_rejects_another_writer_before_fetching_or_starting_a_run(
    store: CandleStore, admin_engine: Engine
) -> None:
    with store.exclusive_writer(), source_with() as source, pytest.raises(IngestionError) as error:
        ingest(source, store, WINDOW, now=lambda: NOW)
    assert error.value.code == ErrorCode.CONCURRENT_JOB
    assert not runs(admin_engine)
    # Lock was released after the failed contender and after leaving the owning context.
    with source_with() as source:
        assert ingest(source, store, WINDOW, now=lambda: NOW)[0].inserted == 3


def test_unclosed_requested_window_is_rejected_before_writing(
    store: CandleStore, admin_engine: Engine
) -> None:
    with source_with() as source, pytest.raises(ValueError):
        ingest(source, store, WINDOW, now=lambda: WINDOW.end)
    assert not runs(admin_engine)


def test_interruption_marks_chunk_failed_and_preserves_resume_information(
    store: CandleStore, admin_engine: Engine
) -> None:
    def interrupt(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    with source_with(interrupt) as source, pytest.raises(IngestionError) as error:
        ingest(source, store, WINDOW, now=lambda: NOW)
    assert error.value.code == ErrorCode.INTERRUPTED and error.value.audit_recorded
    assert error.value.window == WINDOW and error.value.run_id is not None
    assert runs(admin_engine)[0]["status"] == "failed" and not snapshot(admin_engine)


def test_failed_audit_write_never_claims_success(
    store: CandleStore, admin_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(run_id: UUID, finished_at: datetime, code: ErrorCode) -> None:
        raise SQLAlchemyError("synthetic database unavailability")

    monkeypatch.setattr(store, "fail_run", unavailable)
    with (
        source_with(lambda request: httpx.Response(400)) as source,
        pytest.raises(IngestionError) as error,
    ):
        ingest(source, store, WINDOW, now=lambda: NOW)
    assert not error.value.audit_recorded
    assert runs(admin_engine)[0]["status"] == "running" and not snapshot(admin_engine)


def test_empty_chunk_succeeds_with_explicit_missing_coverage(
    store: CandleStore, admin_engine: Engine
) -> None:
    with source_with(lambda request: httpx.Response(200, json=[])) as source:
        report = ingest(source, store, WINDOW, now=lambda: NOW)[0]
    assert report.received == 0 and report.missing_buckets == WINDOW.expected
    assert not snapshot(admin_engine)
    assert runs(admin_engine)[0]["status"] == "succeeded"


def test_chunk_deadline_leaves_no_candles_and_records_failed_audit(
    store: CandleStore, admin_engine: Engine
) -> None:
    elapsed = 0.0

    def too_slow(request: httpx.Request) -> httpx.Response:
        nonlocal elapsed
        elapsed = 11
        return httpx.Response(200, json=wire_rows(request))

    with (
        source_with(too_slow, monotonic=lambda: elapsed) as source,
        pytest.raises(IngestionError) as error,
    ):
        ingest(
            source,
            store,
            WINDOW,
            now=lambda: NOW,
            monotonic=lambda: elapsed,
            chunk_seconds=10,
        )
    assert error.value.code == ErrorCode.DEADLINE_EXCEEDED and error.value.audit_recorded
    assert not snapshot(admin_engine)
    assert runs(admin_engine)[0]["status"] == "failed"


def test_command_deadline_between_months_keeps_completed_month_for_resume(
    store: CandleStore, admin_engine: Engine
) -> None:
    elapsed = 0.0

    def after_chunk(report: object) -> None:
        nonlocal elapsed
        elapsed = 2

    window = TimeWindow(
        MONTH_BOUNDARY - timedelta(minutes=5), MONTH_BOUNDARY + timedelta(minutes=5)
    )
    with source_with(monotonic=lambda: elapsed) as source, pytest.raises(IngestionError) as error:
        ingest(
            source,
            store,
            window,
            now=lambda: NOW,
            monotonic=lambda: elapsed,
            max_seconds=1,
            progress=after_chunk,
        )
    assert error.value.code == ErrorCode.DEADLINE_EXCEEDED
    assert len(snapshot(admin_engine)) == 1 and len(runs(admin_engine)) == 1
