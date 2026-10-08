"""Hand-calculated native metrics, actual reader snapshots, pagination, and HTTP evidence."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.api import create_app
from market_intelligence.config import ApiSettings, DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.hyperliquid_store import HyperliquidStore
from market_intelligence.db.tables import funding_events
from market_intelligence.hyperliquid.models import (
    FundingEvent,
    OpenInterestSnapshot,
    funding_windows,
)
from market_intelligence.hyperliquid.queries import HyperliquidQueries
from market_intelligence.queries.models import QueryValidationError

pytestmark = pytest.mark.integration
START = datetime(2024, 1, 1, tzinfo=UTC)
END = START + timedelta(hours=4)
NOW = datetime(2026, 10, 7, tzinfo=UTC)
RATES = [Decimal("0.0001"), Decimal("-0.00005"), Decimal(0), Decimal("0.0002")]


@pytest.fixture
def reader_queries(database_settings: DatabaseSettings) -> Iterator[HyperliquidQueries]:
    engine = create_db_engine(database_settings, DatabaseRole.READ)
    try:
        settings = ApiSettings(_env_file=None)  # type: ignore[call-arg]
        yield HyperliquidQueries(engine, settings, lambda: NOW + timedelta(days=1))
    finally:
        engine.dispose()


def load_funding(store: HyperliquidStore, rates: list[Decimal] | None = None) -> None:
    window = funding_windows(START, END)[0]
    run_id = store.start_funding(window, NOW)
    values = RATES if rates is None else rates
    store.persist_funding(
        run_id,
        window,
        [
            FundingEvent(START + timedelta(hours=index, milliseconds=76), rate, Decimal(0))
            for index, rate in enumerate(values)
        ],
        NOW,
    )


def load_oi(
    store: HyperliquidStore, received: datetime = NOW, identity: UUID | None = None
) -> UUID:
    run_id = store.start_open_interest(received)
    snapshot = OpenInterestSnapshot(
        identity or uuid4(),
        received,
        received,
        Decimal(0),
        Decimal("42000.125"),
        Decimal("42001.25"),
    )
    store.persist_open_interest(run_id, snapshot, received)
    return snapshot.snapshot_id


def test_exact_metrics_units_offsets_and_whole_window_pagination(
    hyperliquid_store: HyperliquidStore, reader_queries: HyperliquidQueries
) -> None:
    load_funding(hyperliquid_store)
    summary = reader_queries.funding_summary(START, END)
    assert summary.coverage.status == "complete" and summary.coverage.expected_hours == 4
    assert summary.rate_sum == Decimal("0.00025")
    assert summary.rate_sum_percent == Decimal("0.025")
    assert summary.mean_rate == Decimal("0.0000625")
    assert (summary.instrument.denomination_asset, summary.instrument.collateral_asset) == (
        "USDT",
        "USDC",
    )
    first = reader_queries.funding_page(START, END, limit=2)
    assert first.next_cursor and len(first.events) == 2
    assert first.events[0].event_at == START + timedelta(milliseconds=76)
    second = reader_queries.funding_page(START, END, limit=2, cursor=first.next_cursor)
    assert second.coverage == first.coverage and second.next_cursor is None
    assert second.events[0].funding_rate == 0
    assert reader_queries.latest_funding().status == "stale"
    fresh = HyperliquidQueries(reader_queries.engine, now=lambda: END)
    assert fresh.latest_funding().status == "fresh"
    assert fresh.latest_funding().age_seconds == Decimal("3599.924")
    with pytest.raises(QueryValidationError):
        reader_queries.funding_page(START + timedelta(hours=1), END, cursor=first.next_cursor)


def test_missing_hour_withholds_full_window_metrics(
    hyperliquid_store: HyperliquidStore, reader_queries: HyperliquidQueries, admin_engine: Engine
) -> None:
    load_funding(hyperliquid_store)
    with admin_engine.begin() as conn:
        conn.execute(
            funding_events.delete().where(
                funding_events.c.settlement_hour == START + timedelta(hours=1)
            )
        )
    summary = reader_queries.funding_summary(START, END)
    assert summary.coverage.status == "incomplete" and summary.coverage.missing_hours == 1
    gap = summary.coverage.missing_ranges[0]
    assert (gap.start, gap.end, gap.missing_hours) == (
        START + timedelta(hours=1),
        START + timedelta(hours=2),
        1,
    )
    assert summary.rate_sum is summary.rate_sum_percent is summary.mean_rate is None


def test_gap_ranges_are_bounded(
    hyperliquid_store: HyperliquidStore, reader_queries: HyperliquidQueries
) -> None:
    end = START + timedelta(hours=130)
    window = funding_windows(START, end)[0]
    run_id = hyperliquid_store.start_funding(window, NOW)
    events = [
        FundingEvent(START + timedelta(hours=index, milliseconds=76), Decimal(0), Decimal(0))
        for index in range(1, 130, 2)
    ]
    hyperliquid_store.persist_funding(run_id, window, events, NOW)
    coverage = reader_queries.funding_page(START, end, limit=1).coverage
    assert coverage.missing_hours == 65 and coverage.missing_range_count == 65
    assert coverage.missing_ranges_truncated and len(coverage.missing_ranges) == 50


def test_no_data_has_explicit_gap_and_no_invented_oi_history(
    hyperliquid_store: HyperliquidStore, reader_queries: HyperliquidQueries
) -> None:
    summary = reader_queries.funding_summary(START, END)
    assert summary.coverage.status == "no_data" and summary.coverage.missing_hours == 4
    assert summary.rate_sum is None
    assert reader_queries.latest_funding().event is None
    assert reader_queries.latest_open_interest().snapshot is None
    page = reader_queries.open_interest_page(START, END)
    assert (
        page.coverage.observed_snapshots == 0 and page.historical_completeness == "not_established"
    )


def test_oi_same_receipt_time_tie_break_and_half_open_bounds(
    hyperliquid_store: HyperliquidStore, reader_queries: HyperliquidQueries
) -> None:
    identities = [UUID(int=2), UUID(int=1)]
    for identity in identities:
        load_oi(hyperliquid_store, identity=identity)
    load_oi(hyperliquid_store, NOW + timedelta(hours=1), UUID(int=3))
    first = reader_queries.open_interest_page(NOW, NOW + timedelta(hours=1), limit=1)
    assert first.coverage.observed_snapshots == 2 and first.snapshots[0].snapshot_id == UUID(int=1)
    assert first.next_cursor
    second = reader_queries.open_interest_page(
        NOW, NOW + timedelta(hours=1), limit=1, cursor=first.next_cursor
    )
    assert second.snapshots[0].snapshot_id == UUID(int=2) and second.next_cursor is None
    assert second.coverage == first.coverage
    assert first.snapshots[0].source_event_at is None and first.snapshots[0].open_interest_btc == 0
    assert reader_queries.latest_open_interest().status == "stale"
    fresh = HyperliquidQueries(reader_queries.engine, now=lambda: NOW + timedelta(hours=1))
    assert fresh.latest_open_interest().status == "fresh"
    with pytest.raises(QueryValidationError):
        reader_queries.funding_page(START, END, cursor=first.next_cursor)


@pytest.mark.parametrize(
    "start,end",
    [
        (START.replace(tzinfo=None), END),
        (END, START),
        (START + timedelta(milliseconds=1), END),
        (START, END + timedelta(minutes=1)),
    ],
)
def test_invalid_funding_windows_fail_before_reader_checkout(
    reader_queries: HyperliquidQueries,
    start: datetime,
    end: datetime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden() -> None:
        pytest.fail("Reader checkout should not happen")

    monkeypatch.setattr(reader_queries.engine, "connect", forbidden)
    with pytest.raises(QueryValidationError):
        reader_queries.funding_page(start, end)


def test_concurrent_correction_keeps_summary_snapshot(
    hyperliquid_store: HyperliquidStore, reader_queries: HyperliquidQueries
) -> None:
    load_funding(hyperliquid_store)
    corrected = [Decimal("0.001"), *RATES[1:]]
    fired = False

    def correction(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        nonlocal fired
        if not fired and statement.startswith("SELECT count(") and "funding_events" in statement:
            fired = True
            load_funding(hyperliquid_store, corrected)

    sa.event.listen(reader_queries.engine, "after_cursor_execute", correction)
    try:
        summary = reader_queries.funding_summary(START, END)
        assert fired and summary.rate_sum == Decimal("0.00025")
    finally:
        sa.event.remove(reader_queries.engine, "after_cursor_execute", correction)
    assert reader_queries.funding_summary(START, END).rate_sum == Decimal("0.00115")


def test_http_exact_evidence_and_sanitized_failures(
    hyperliquid_store: HyperliquidStore,
    reader_queries: HyperliquidQueries,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_funding(hyperliquid_store)
    load_oi(hyperliquid_store)
    app = create_app(
        engine=reader_queries.engine, api_settings=reader_queries.settings, now=reader_queries.now
    )
    with TestClient(app) as client:
        params = {"start": START.isoformat(), "end": END.isoformat(), "limit": 2}
        response = client.get("/v1/hyperliquid/funding", params=params)
        assert response.status_code == 200
        body = response.json()
        assert body["events"][0]["funding_rate"] == "0.000100000000000000"
        assert body["events"][0]["event_at"] == "2024-01-01T00:00:00.076000Z"
        assert body["instrument"]["code"] == "BTC-PERP"
        assert (
            client.get("/v1/hyperliquid/funding/summary", params=params).json()["rate_sum_percent"]
            == "0.025000000000000000"
        )
        assert (
            client.get("/v1/hyperliquid/open-interest/latest").json()["snapshot"]["source_event_at"]
            is None
        )
        assert (
            client.get("/v1/hyperliquid/funding", params={**params, "limit": 0}).status_code == 422
        )
        assert (
            client.get("/v1/hyperliquid/funding", params={**params, "cursor": "bad"}).status_code
            == 422
        )
        assert (
            client.get(
                "/v1/hyperliquid/open-interest",
                params={"start": START.isoformat(), "end": END.isoformat()},
            ).status_code
            == 200
        )

        def broken() -> None:
            raise SQLAlchemyError("private database credentials or SQL")

        monkeypatch.setattr(app.state.hyperliquid_queries, "latest_funding", broken)
        failure = client.get("/v1/hyperliquid/funding/latest")
        assert failure.status_code == 503 and failure.json() == {"detail": "Database unavailable"}
