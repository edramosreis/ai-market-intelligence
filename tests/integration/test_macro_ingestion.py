"""Real macro clients and writer transactions with synthetic bounded HTTP responses."""

import calendar
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool

from market_intelligence.db.macro_store import MacroStore
from market_intelligence.db.macro_tables import macro_current as current
from market_intelligence.db.macro_tables import macro_ingestion_runs as runs
from market_intelligence.db.macro_tables import macro_observed_versions as versions
from market_intelligence.macro.bls import BlsClient
from market_intelligence.macro.fed import FedClient
from market_intelligence.macro.jobs import MacroIngestionError
from market_intelligence.macro.models import MacroProvider, MacroSeries, MonthlyWindow
from market_intelligence.macro.service import ingest_macro
from market_intelligence.macro.storage_models import MacroStorageError, MacroStorageErrorCode
from tests.unit.test_fed import archive, document

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 8, tzinfo=UTC)
OLD = MonthlyWindow(date(2020, 1, 1), date(2020, 3, 1))


class Clock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def now(self) -> datetime:
        return NOW + timedelta(seconds=self.elapsed)

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


def bls_body(
    request: httpx.Request,
    *,
    omit: tuple[str, int, int] | None = None,
    dash: bool = False,
    value: str = "10.125",
) -> dict[str, Any]:
    arguments = json.loads(request.content)
    first, last = int(arguments["startyear"]), int(arguments["endyear"])
    series = []
    for identity in arguments["seriesid"]:
        rows = []
        for year in range(first, last + 1):
            if identity == MacroSeries.UNEMPLOYMENT.value and year < 1948:
                continue
            for month in range(1, 13):
                if omit == (identity, year, month):
                    continue
                missing = dash and identity == MacroSeries.CPI.value and month == 1
                rows.append(
                    {
                        "year": str(year),
                        "period": f"M{month:02d}",
                        "periodName": calendar.month_name[month],
                        "value": "-"
                        if missing
                        else (value if identity == MacroSeries.CPI.value else "0"),
                        "footnotes": [{"code": "X", "text": "Synthetic unavailable"}]
                        if missing
                        else [{}],
                    }
                )
        series.append({"seriesID": identity, "data": rows})
    return {
        "status": "REQUEST_SUCCEEDED",
        "responseTime": 1,
        "message": [],
        "Results": {"series": series},
    }


def run_bls(
    store: MacroStore,
    clock: Clock,
    window: MonthlyWindow = OLD,
    *,
    resume: bool = False,
    omit: tuple[str, int, int] | None = None,
    dash: bool = False,
    value: str = "10.125",
) -> tuple[int, bool]:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=bls_body(request, omit=omit, dash=dash, value=value)
            )
        )
    ) as http:
        source = BlsClient(http, now=clock.now, monotonic=clock.monotonic, sleep=clock.sleep)
        reports = ingest_macro(
            source,
            store,
            MacroProvider.BLS,
            window,
            resume=resume,
            now=clock.now,
            monotonic=clock.monotonic,
        )
    clock.elapsed += 10
    return source.requests_used, reports[0].skipped


def test_real_bls_reader_then_store_has_no_held_connection_during_http(
    macro_store: MacroStore,
) -> None:
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        assert isinstance(macro_store.engine.pool, QueuePool)
        assert macro_store.engine.pool.checkedout() == 0
        body = bls_body(request, dash=True)
        for series in body["Results"]["series"]:
            series["data"].append(
                {
                    "year": "2020",
                    "period": "M13",
                    "periodName": "Annual",
                    "value": "1",
                    "footnotes": [{}],
                }
            )
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        reports = ingest_macro(
            BlsClient(http, now=clock.now, monotonic=clock.monotonic),
            macro_store,
            MacroProvider.BLS,
            OLD,
            resume=True,
            now=clock.now,
            monotonic=clock.monotonic,
        )
    assert reports[0].inserted == 4 and reports[0].source_missing == 1
    assert reports[0].annual_average_count == 2
    with macro_store.engine.connect() as conn:
        assert conn.execute(
            sa.select(versions.c.value).where(
                versions.c.series_id == MacroSeries.CPI.value,
                versions.c.observation_month == date(2020, 2, 1),
            )
        ).scalar_one() == Decimal("10.125")
        assert conn.execute(sa.select(runs.c.status)).scalar_one() == "succeeded"
    assert run_bls(macro_store, clock, dash=True, resume=True) == (0, True)


def test_resume_refetches_absent_months_then_skips_a_complete_read(macro_store: MacroStore) -> None:
    clock = Clock()
    assert run_bls(macro_store, clock, omit=(MacroSeries.CPI.value, 2020, 1)) == (1, False)
    assert run_bls(macro_store, clock, resume=True) == (1, False)
    assert run_bls(macro_store, clock, resume=True) == (0, True)


def test_retained_omissions_do_not_make_an_incomplete_read_resumable(
    macro_store: MacroStore,
) -> None:
    clock = Clock()
    run_bls(macro_store, clock)
    run_bls(macro_store, clock, omit=(MacroSeries.CPI.value, 2020, 1))
    with macro_store.engine.connect() as conn:
        latest = (
            conn.execute(sa.select(runs).order_by(runs.c.finished_at.desc())).mappings().first()
        )
        assert latest is not None and (latest["received"], latest["retained"]) == (3, 1)
    assert run_bls(macro_store, clock, resume=True) == (1, False)


@pytest.mark.parametrize("status", ["failed", "running", "partial_success"])
def test_later_overlapping_attempts_prevent_reusing_an_older_audit(
    macro_store: MacroStore,
    status: str,
) -> None:
    clock = Clock()
    run_bls(macro_store, clock)
    partial = MonthlyWindow(OLD.start, date(2020, 2, 1))
    if status == "partial_success":
        run_bls(macro_store, clock, partial)
    else:
        identity = macro_store.start_run(MacroProvider.BLS, partial, clock.now())
        if status == "failed":
            from market_intelligence.macro.storage_models import MacroFailureCode

            macro_store.fail_run(identity, MacroFailureCode.HTTP_ERROR, clock.now())
    assert run_bls(macro_store, clock, resume=True) == (1, False)


def test_resume_verifies_stored_keys_instead_of_only_audit_counts(
    macro_store: MacroStore,
    admin_engine: Engine,
) -> None:
    run_bls(macro_store, Clock())
    with admin_engine.begin() as conn:
        conn.execute(
            current.delete().where(
                current.c.series_id == MacroSeries.CPI.value,
                current.c.observation_month == OLD.start,
            )
        )
    assert macro_store.completed(MacroProvider.BLS, OLD) is None


def test_bls_resume_replays_the_current_five_year_revision_region(macro_store: MacroStore) -> None:
    clock = Clock()
    recent = MonthlyWindow(date(2024, 1, 1), date(2024, 3, 1))
    assert run_bls(macro_store, clock, recent) == (1, False)
    assert run_bls(macro_store, clock, recent, resume=True, value="11") == (1, False)
    with macro_store.engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(versions)).scalar_one() == 6


@pytest.mark.parametrize("failure", ["http", "payload", "interrupt", "deadline"])
def test_provider_failures_are_sanitized_and_recorded_separately(
    macro_store: MacroStore,
    failure: str,
) -> None:
    clock = Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "interrupt":
            raise KeyboardInterrupt
        if failure == "deadline":
            clock.elapsed = 200
        return httpx.Response(400 if failure == "http" else 200, content=b"Private invalid payload")

    codes = {
        "http": "http_error",
        "payload": "invalid_payload",
        "interrupt": "interrupted",
        "deadline": "deadline_exceeded",
    }
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        pytest.raises(MacroIngestionError, match=f"^{codes[failure]}$") as caught,
    ):
        ingest_macro(
            BlsClient(http, now=clock.now, monotonic=clock.monotonic),
            macro_store,
            MacroProvider.BLS,
            OLD,
            now=clock.now,
            monotonic=clock.monotonic,
        )
    assert caught.value.audit_recorded and caught.value.run_id is not None
    with macro_store.engine.connect() as conn:
        assert conn.execute(sa.select(runs.c.error_code)).scalar_one() == codes[failure]
        assert conn.execute(sa.select(sa.func.count()).select_from(versions)).scalar_one() == 0


def test_later_bls_request_budget_failure_keeps_the_first_committed_chunk(
    macro_store: MacroStore,
) -> None:
    clock = Clock()
    window = MonthlyWindow(date(2000, 1, 1), date(2011, 1, 1))
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=bls_body(request)))
    ) as http:
        source = BlsClient(http, max_requests=1, now=clock.now, monotonic=clock.monotonic)
        with pytest.raises(MacroIngestionError, match="^retry_exhausted$") as caught:
            ingest_macro(
                source,
                macro_store,
                MacroProvider.BLS,
                window,
                now=clock.now,
                monotonic=clock.monotonic,
            )
    assert source.requests_used == 1 and caught.value.audit_recorded
    assert caught.value.window == MonthlyWindow(date(2010, 1, 1), window.end)
    with macro_store.engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(current)).scalar_one() == 240
        assert conn.execute(
            sa.select(runs.c.status).order_by(runs.c.requested_start)
        ).scalars().all() == ["succeeded", "failed"]


def test_storage_rollback_and_failed_audit_do_not_mask_the_original_error(
    macro_store: MacroStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_audit(*args: object) -> None:
        if str(args[2]).startswith("UPDATE macro_ingestion_runs"):
            raise SQLAlchemyError("Private SQL credentials")

    monkeypatch.setattr(
        macro_store,
        "fail_run",
        Mock(side_effect=MacroStorageError(MacroStorageErrorCode.DATABASE_ERROR)),
    )
    sa.event.listen(macro_store.engine, "before_cursor_execute", fail_audit)
    try:
        with pytest.raises(MacroIngestionError, match="^database_error$") as caught:
            run_bls(macro_store, Clock())
    finally:
        sa.event.remove(macro_store.engine, "before_cursor_execute", fail_audit)
    assert (
        not caught.value.audit_recorded
        and caught.value.storage_code == MacroStorageErrorCode.DATABASE_ERROR
    )
    with macro_store.engine.connect() as conn:
        assert conn.execute(sa.select(runs.c.status)).scalar_one() == "running"
        assert conn.execute(sa.select(sa.func.count()).select_from(versions)).scalar_one() == 0


def test_fed_reads_one_full_release_for_the_whole_requested_window(macro_store: MacroStore) -> None:
    clock = Clock()
    window = MonthlyWindow(date(2024, 1, 1), date(2024, 3, 1))
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=archive(document())))
    ) as http:
        source = FedClient(http, now=clock.now, monotonic=clock.monotonic)
        first = ingest_macro(
            source, macro_store, MacroProvider.FED, window, now=clock.now, monotonic=clock.monotonic
        )
        second = ingest_macro(
            source,
            macro_store,
            MacroProvider.FED,
            window,
            resume=True,
            now=clock.now,
            monotonic=clock.monotonic,
        )
    assert source.requests_used == 1 and first[0].inserted == 2 and second[0].skipped
    with macro_store.engine.connect() as conn:
        assert conn.execute(
            sa.select(versions.c.native_period).order_by(versions.c.observation_month)
        ).scalars().all() == ["2024-01-31", "2024-02-29"]


def test_fed_latest_completed_month_is_refetched_on_resume(macro_store: MacroStore) -> None:
    clock = Clock()
    window = MonthlyWindow(date(2026, 9, 1), date(2026, 10, 1))
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=archive(document())))
    ) as http:
        source = FedClient(http, now=clock.now, monotonic=clock.monotonic, sleep=clock.sleep)
        for _ in range(2):
            reports = ingest_macro(
                source,
                macro_store,
                MacroProvider.FED,
                window,
                resume=True,
                now=clock.now,
                monotonic=clock.monotonic,
            )
            assert not reports[0].skipped and reports[0].received == 0
    assert source.requests_used == 2
