"""Actual writer transactions, atomicity, retries, and independent Hyperliquid locks."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool

from market_intelligence.db.hyperliquid_store import HyperliquidStore
from market_intelligence.db.tables import (
    funding_events,
    funding_ingestion_runs,
    open_interest_runs,
    open_interest_snapshots,
)
from market_intelligence.db.treasury_store import TreasuryStore
from market_intelligence.hyperliquid.client import HyperliquidClient
from market_intelligence.hyperliquid.models import (
    FundingEvent,
    HyperliquidIngestionError,
    funding_windows,
    milliseconds,
)
from market_intelligence.hyperliquid.service import collect_open_interest, ingest_funding

pytestmark = pytest.mark.integration
START = datetime(2024, 1, 1, tzinfo=UTC)
END = START + timedelta(hours=3)
NOW = datetime(2026, 10, 7, tzinfo=UTC)


def rows(count: int = 3, changed: bool = False) -> list[dict[str, Any]]:
    return [
        dict(
            coin="BTC",
            time=milliseconds(START + timedelta(hours=index, milliseconds=76)),
            fundingRate="-0.01" if changed and index == 1 else "0.0000125",
            premium="0",
        )
        for index in range(count)
    ]


def test_replay_corrections_retained_events_and_gap_aware_resume(
    hyperliquid_store: HyperliquidStore, admin_engine: Engine
) -> None:
    clock = [0.0]
    payload = rows()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # Only the advisory-lock connection is held; no write transaction spans HTTP.
        assert cast(QueuePool, hyperliquid_store.engine.pool).checkedout() == 1
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        source = HyperliquidClient(http, request_interval=0, monotonic=lambda: clock[0])

        def run(resume: bool = False) -> Any:
            clock[0] += 60
            return ingest_funding(
                source,
                hyperliquid_store,
                START,
                END,
                resume=resume,
                now=lambda: NOW + timedelta(seconds=clock[0]),
                monotonic=lambda: clock[0],
            )[0]

        first = run()
        assert first.inserted == 3 and first.missing_hours == 0
        with admin_engine.connect() as conn:
            original = (
                conn.execute(sa.select(funding_events).order_by(funding_events.c.event_at))
                .mappings()
                .all()
            )
        replay = run()
        assert replay.unchanged == 3
        with admin_engine.connect() as conn:
            assert (
                conn.execute(sa.select(funding_events).order_by(funding_events.c.event_at))
                .mappings()
                .all()
                == original
            )
        assert run(True).skipped and calls == 2
        payload = rows(changed=True)
        correction = run()
        assert (correction.updated, correction.unchanged) == (1, 2)
        with admin_engine.connect() as conn:
            changed = (
                conn.execute(
                    sa.select(funding_events).where(
                        funding_events.c.settlement_hour == START + timedelta(hours=1)
                    )
                )
                .mappings()
                .one()
            )
            assert changed["funding_rate"] == Decimal("-0.01")
            assert changed["first_ingested_at"] == original[1]["first_ingested_at"]
            assert changed["last_ingestion_run_id"] == correction.run_id
        payload = rows(1)
        omitted = run()
        assert omitted.retained == 2 and omitted.missing_hours == 0
        assert not run(True).skipped  # Source read was gapped despite retained local values.
        payload = rows(changed=True)
        payload[1]["time"] += 1
        with pytest.raises(HyperliquidIngestionError, match="^invalid_payload$") as caught:
            run()
        assert caught.value.audit_recorded
        with admin_engine.connect() as conn:
            assert (
                conn.execute(sa.select(sa.func.count()).select_from(funding_events)).scalar_one()
                == 3
            )


def test_empty_history_is_refetched_and_failed_latest_attempt_is_not_hidden(
    hyperliquid_store: HyperliquidStore,
) -> None:
    clock = [0.0]
    status = [200]
    payload: list[dict[str, Any]] = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status[0], json=payload))
    ) as http:
        source = HyperliquidClient(http, request_interval=0, attempts=1)

        def run() -> Any:
            clock[0] += 1
            return ingest_funding(
                source,
                hyperliquid_store,
                START,
                END,
                resume=True,
                now=lambda: NOW + timedelta(seconds=clock[0]),
            )[0]

        assert run().missing_hours == 3
        assert not run().skipped
        payload.extend(rows())
        assert not run().skipped
        status[0] = 400
        # Explicit historical replay fails; the next resume must not reuse its older success.
        with pytest.raises(HyperliquidIngestionError):
            ingest_funding(
                source, hyperliquid_store, START, END, now=lambda: NOW + timedelta(seconds=10)
            )
        status[0] = 200
        clock[0] = 11
        assert not run().skipped


def test_later_month_failure_preserves_earlier_commit_and_retry(
    hyperliquid_store: HyperliquidStore, admin_engine: Engine
) -> None:
    windows = funding_windows(START, datetime(2024, 3, 1, tzinfo=UTC))
    status = [400]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["startTime"] >= milliseconds(windows[1].start):
            return httpx.Response(status[0], json=[])
        return httpx.Response(200, json=rows())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        source = HyperliquidClient(http, request_interval=0)
        with pytest.raises(HyperliquidIngestionError) as caught:
            ingest_funding(source, hyperliquid_store, START, windows[-1].end, now=lambda: NOW)
        assert caught.value.window == windows[1] and caught.value.audit_recorded
        with admin_engine.connect() as conn:
            assert (
                conn.execute(sa.select(sa.func.count()).select_from(funding_events)).scalar_one()
                == 3
            )
            statuses = (
                conn.execute(
                    sa.select(funding_ingestion_runs.c.status).order_by(
                        funding_ingestion_runs.c.requested_start
                    )
                )
                .scalars()
                .all()
            )
            assert statuses == ["succeeded", "failed"]
        status[0] = 200
        report = ingest_funding(
            source,
            hyperliquid_store,
            windows[1].start,
            windows[1].end,
            now=lambda: NOW + timedelta(minutes=1),
        )
        assert report[0].received == 0 and report[0].missing_hours == 696


def test_fact_success_audit_rollback_is_atomic(
    hyperliquid_store: HyperliquidStore, admin_engine: Engine
) -> None:
    def reject_success(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if (
            statement.startswith("UPDATE funding_ingestion_runs")
            and parameters.get("status") == "succeeded"
        ):
            raise SQLAlchemyError("injected private database details")

    sa.event.listen(hyperliquid_store.engine, "before_cursor_execute", reject_success)
    try:
        with (
            httpx.Client(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, json=rows()))
            ) as http,
            pytest.raises(HyperliquidIngestionError, match="^database_error$") as caught,
        ):
            ingest_funding(HyperliquidClient(http), hyperliquid_store, START, END, now=lambda: NOW)
        assert caught.value.audit_recorded
        with admin_engine.connect() as conn:
            assert (
                conn.execute(sa.select(sa.func.count()).select_from(funding_events)).scalar_one()
                == 0
            )
            assert conn.execute(sa.select(funding_ingestion_runs.c.status)).scalar_one() == "failed"
    finally:
        sa.event.remove(hyperliquid_store.engine, "before_cursor_execute", reject_success)


def test_oi_is_append_only_and_provider_failure_audited(
    hyperliquid_store: HyperliquidStore, admin_engine: Engine
) -> None:
    payload = [
        {"universe": [{"name": "BTC"}]},
        [{"openInterest": "0", "markPx": "42000", "oraclePx": "42001"}],
    ]
    status = [200]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status[0], json=payload))
    ) as http:
        source = HyperliquidClient(http, request_interval=0, now=lambda: NOW)
        first = collect_open_interest(source, hyperliquid_store, now=lambda: NOW)[1]
        second = collect_open_interest(source, hyperliquid_store, now=lambda: NOW)[1]
        assert first.snapshot_id != second.snapshot_id
        status[0] = 400
        with pytest.raises(HyperliquidIngestionError) as caught:
            collect_open_interest(source, hyperliquid_store, now=lambda: NOW)
        assert caught.value.audit_recorded
    with admin_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(open_interest_snapshots)
            ).scalar_one()
            == 2
        )
        assert set(conn.execute(sa.select(open_interest_runs.c.status)).scalars()) == {
            "succeeded",
            "failed",
        }


def test_locks_exclude_same_job_but_allow_independent_domains(
    hyperliquid_store: HyperliquidStore,
) -> None:
    with hyperliquid_store.exclusive_writer("funding"):
        with (
            pytest.raises(HyperliquidIngestionError, match="^concurrent_job$"),
            hyperliquid_store.exclusive_writer("funding"),
        ):
            pytest.fail("Concurrent funding job acquired lock")
        with (
            hyperliquid_store.exclusive_writer("open_interest"),
            TreasuryStore(hyperliquid_store.engine).exclusive_writer(),
        ):
            pass
    with hyperliquid_store.exclusive_writer("funding"):
        pass


def test_invalid_in_memory_batch_does_not_change_facts(hyperliquid_store: HyperliquidStore) -> None:
    window = funding_windows(START, END)[0]
    run_id = hyperliquid_store.start_funding(window, NOW)
    event = FundingEvent(START + timedelta(milliseconds=76), Decimal(0), Decimal(0))
    with pytest.raises(HyperliquidIngestionError):
        hyperliquid_store.persist_funding(run_id, window, [event, event], NOW)
