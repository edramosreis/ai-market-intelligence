"""Real monthly transactions/roles with synthetic Treasury XML and injected HTTP failures."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.candle_store import CandleStore
from market_intelligence.db.tables import treasury_ingestion_runs as runs
from market_intelligence.db.tables import treasury_yields as yields
from market_intelligence.db.treasury_store import TreasuryStore
from market_intelligence.treasury.client import TreasuryClient
from market_intelligence.treasury.models import TreasuryErrorCode, TreasuryIngestionError
from market_intelligence.treasury.service import ingest_treasury
from tests.unit.test_treasury import Clock, entry, feed

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 6, tzinfo=UTC)
START, END = date(2024, 1, 1), date(2024, 2, 1)
FIELDS = "<d:BC_2YEAR>4.25</d:BC_2YEAR><d:BC_10YEAR>4.125</d:BC_10YEAR>"


def wire(request: httpx.Request) -> bytes:
    month = request.url.params["field_tdr_date_value_month"]
    prefix = f"{month[:4]}-{month[4:]}"
    return feed(entry(prefix + "-02", FIELDS) + entry(prefix + "-03", FIELDS))


@contextmanager
def source_with(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    **options: Any,
) -> Iterator[TreasuryClient]:
    with httpx.Client(
        transport=httpx.MockTransport(
            handler or (lambda request: httpx.Response(200, content=wire(request)))
        )
    ) as http:
        yield TreasuryClient(http, request_interval=0, attempts=1, **options)


def snapshot(engine: Engine, table: sa.Table = yields) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        statement = sa.select(table)
        if table is yields:
            statement = statement.order_by(yields.c.observed_on, yields.c.tenor)
        return [dict(row) for row in conn.execute(statement).mappings()]


def test_replay_preserves_provenance_and_reports_native_missingness(
    treasury_store: TreasuryStore,
    admin_engine: Engine,
) -> None:
    with source_with() as source:
        first = ingest_treasury(source, treasury_store, START, END, now=lambda: NOW)[0]
    before = snapshot(admin_engine)
    assert (first.received_dates, first.received_rates, first.inserted) == (2, 28, 28)
    assert first.field_absent == 24 and first.source_null == 0
    with source_with() as source:
        replay = ingest_treasury(
            source, treasury_store, START, END, now=lambda: NOW + timedelta(hours=1)
        )[0]
    assert (replay.inserted, replay.updated, replay.unchanged) == (0, 0, 28)
    assert snapshot(admin_engine) == before
    assert len(snapshot(admin_engine, runs)) == 2


def test_corrections_replace_values_and_null_reasons_only_where_changed(
    treasury_store: TreasuryStore,
    admin_engine: Engine,
) -> None:
    with source_with() as source:
        ingest_treasury(source, treasury_store, START, END, now=lambda: NOW)
    before = snapshot(admin_engine)
    changed = feed(
        entry(
            "2024-01-02",
            '<d:BC_2YEAR>0</d:BC_2YEAR><d:BC_10YEAR m:null="true"/><d:BC_20YEAR m:null="true"/>',
        )
        + entry("2024-01-03", FIELDS)
    )
    with source_with(lambda request: httpx.Response(200, content=changed)) as source:
        report = ingest_treasury(
            source, treasury_store, START, END, now=lambda: NOW + timedelta(hours=1)
        )[0]
    assert (report.inserted, report.updated, report.unchanged) == (0, 3, 25)
    after = snapshot(admin_engine)
    corrected = [row for row in after if row["last_ingestion_run_id"] == report.run_id]
    assert len(corrected) == 3 and report.source_null == 2
    assert next(row for row in corrected if row["tenor"] == "2Y")["yield_percent"] == Decimal(0)
    for old, new in zip(before, after, strict=True):
        assert old["first_ingested_at"] == new["first_ingested_at"]
        if new not in corrected:
            assert old == new


def test_omitted_dates_are_retained_without_new_provenance(
    treasury_store: TreasuryStore,
    admin_engine: Engine,
) -> None:
    with source_with() as source:
        ingest_treasury(source, treasury_store, START, END, now=lambda: NOW)
    before = snapshot(admin_engine)
    with source_with(lambda request: httpx.Response(200, content=feed())) as source:
        report = ingest_treasury(
            source, treasury_store, START, END, now=lambda: NOW + timedelta(days=1)
        )[0]
    assert report.received_dates == 0 and report.retained_dates == 2
    assert snapshot(admin_engine) == before
    with source_with(lambda request: pytest.fail("Historical validated read is reused")) as source:
        skipped = ingest_treasury(source, treasury_store, START, END, resume=True, now=lambda: NOW)[
            0
        ]
    assert skipped.skipped and skipped.retained_dates == 2


def test_later_failure_preserves_earlier_month_and_resume_refetches_failure(
    treasury_store: TreasuryStore,
    admin_engine: Engine,
) -> None:
    def fail_feb(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(400)
            if request.url.params["field_tdr_date_value_month"] == "202402"
            else httpx.Response(200, content=wire(request))
        )

    with source_with(fail_feb) as source, pytest.raises(TreasuryIngestionError) as failure:
        ingest_treasury(source, treasury_store, START, date(2024, 3, 1), now=lambda: NOW)
    assert failure.value.code == TreasuryErrorCode.HTTP_ERROR and failure.value.audit_recorded
    assert len(snapshot(admin_engine)) == 28
    assert sorted(row["status"] for row in snapshot(admin_engine, runs)) == ["failed", "succeeded"]
    fetched = []

    def track(request: httpx.Request) -> httpx.Response:
        fetched.append(request.url.params["field_tdr_date_value_month"])
        return httpx.Response(200, content=wire(request))

    with source_with(track) as source:
        reports = ingest_treasury(
            source, treasury_store, START, date(2024, 3, 1), resume=True, now=lambda: NOW
        )
    assert [report.skipped for report in reports] == [True, False] and fetched == ["202402"]
    assert len(snapshot(admin_engine)) == 56


def test_failed_success_audit_rolls_back_facts(
    treasury_store: TreasuryStore, admin_engine: Engine
) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE FUNCTION test_treasury_reject_success() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN IF NEW.status = 'succeeded' THEN RAISE EXCEPTION 'synthetic failure'; "
                "END IF; RETURN NEW; END $$"
            )
        )
        conn.execute(
            sa.text(
                "CREATE TRIGGER reject_treasury_success BEFORE UPDATE ON treasury_ingestion_runs "
                "FOR EACH ROW EXECUTE FUNCTION test_treasury_reject_success()"
            )
        )
    try:
        with source_with() as source, pytest.raises(TreasuryIngestionError) as failure:
            ingest_treasury(source, treasury_store, START, END, now=lambda: NOW)
        assert (
            failure.value.code == TreasuryErrorCode.DATABASE_ERROR and failure.value.audit_recorded
        )
        assert snapshot(admin_engine) == []
        assert snapshot(admin_engine, runs)[0]["status"] == "failed"
    finally:
        with admin_engine.begin() as conn:
            conn.execute(sa.text("DROP TRIGGER reject_treasury_success ON treasury_ingestion_runs"))
            conn.execute(sa.text("DROP FUNCTION test_treasury_reject_success()"))


def test_current_month_is_always_refetched_with_resume(
    treasury_store: TreasuryStore,
    admin_engine: Engine,
) -> None:
    for _ in range(2):
        with source_with() as source:
            report = ingest_treasury(
                source,
                treasury_store,
                date(2026, 10, 1),
                date(2026, 11, 1),
                resume=True,
                now=lambda: NOW,
            )[0]
        assert not report.skipped
    assert len(snapshot(admin_engine, runs)) == 2


def test_resume_refetches_if_a_normalized_tenor_is_missing(
    treasury_store: TreasuryStore,
    admin_engine: Engine,
) -> None:
    with source_with() as source:
        ingest_treasury(source, treasury_store, START, END, now=lambda: NOW)
    with admin_engine.begin() as conn:
        conn.execute(
            yields.delete().where(yields.c.observed_on == date(2024, 1, 2), yields.c.tenor == "2Y")
        )
    with source_with() as source:
        report = ingest_treasury(source, treasury_store, START, END, resume=True, now=lambda: NOW)[
            0
        ]
    assert not report.skipped and report.inserted == 1 and report.unchanged == 27


def test_no_write_transaction_is_held_during_http(
    treasury_store: TreasuryStore,
    admin_engine: Engine,
    database_settings: DatabaseSettings,
) -> None:
    def inspect(request: httpx.Request) -> httpx.Response:
        with admin_engine.connect() as conn:
            assert (
                conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_stat_activity WHERE usename = :role "
                        "AND state LIKE 'idle in transaction%'"
                    ),
                    {"role": database_settings.credentials(DatabaseRole.INGEST)[0]},
                ).scalar_one()
                == 0
            )
        return httpx.Response(200, content=wire(request))

    with source_with(inspect) as source:
        ingest_treasury(source, treasury_store, START, END, now=lambda: NOW)


def test_treasury_writer_exclusion_is_independent_of_coinbase(
    treasury_store: TreasuryStore,
) -> None:
    with treasury_store.exclusive_writer():
        with pytest.raises(TreasuryIngestionError) as failure, treasury_store.exclusive_writer():
            pass
        assert failure.value.code == TreasuryErrorCode.CONCURRENT_JOB
        with CandleStore(treasury_store.engine).exclusive_writer():
            pass
    with treasury_store.exclusive_writer():
        pass


@pytest.mark.parametrize("failure", [RuntimeError("private response text"), KeyboardInterrupt()])
def test_failure_details_are_sanitized_and_audited(
    treasury_store: TreasuryStore,
    admin_engine: Engine,
    failure: BaseException,
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise failure

    with source_with(fail) as source, pytest.raises(TreasuryIngestionError) as caught:
        ingest_treasury(source, treasury_store, START, END, now=lambda: NOW)
    assert str(caught.value) in {"internal_error", "interrupted"} and caught.value.audit_recorded
    assert snapshot(admin_engine) == [] and snapshot(admin_engine, runs)[0]["status"] == "failed"


def test_future_source_date_is_rejected_and_audited(treasury_store: TreasuryStore) -> None:
    with (
        source_with(
            lambda request: httpx.Response(200, content=feed(entry("2026-10-07", FIELDS)))
        ) as source,
        pytest.raises(TreasuryIngestionError) as caught,
    ):
        ingest_treasury(
            source, treasury_store, date(2026, 10, 1), date(2026, 11, 1), now=lambda: NOW
        )
    assert caught.value.code == TreasuryErrorCode.INVALID_PAYLOAD and caught.value.audit_recorded


def test_deadline_during_http_retains_failed_audit(
    treasury_store: TreasuryStore, admin_engine: Engine
) -> None:
    clock = Clock()

    def late(request: httpx.Request) -> httpx.Response:
        clock.elapsed = 61
        return httpx.Response(200, content=wire(request))

    with (
        source_with(late, monotonic=clock.monotonic, sleep=clock.sleep) as source,
        pytest.raises(TreasuryIngestionError) as caught,
    ):
        ingest_treasury(
            source, treasury_store, START, END, now=lambda: NOW, monotonic=clock.monotonic
        )
    assert caught.value.code == TreasuryErrorCode.DEADLINE_EXCEEDED and caught.value.audit_recorded
    assert snapshot(admin_engine) == []
