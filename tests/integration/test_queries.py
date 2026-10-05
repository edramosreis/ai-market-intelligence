"""Analytical/HTTP checks using actual reader credentials and isolated PostgreSQL."""

from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from market_intelligence.api import create_app
from market_intelligence.config import ApiSettings, DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.tables import candles, ingestion_runs
from market_intelligence.ingestion.models import INTERVAL, parse_instant
from market_intelligence.queries.models import QueryValidationError, UnknownMarketError
from market_intelligence.queries.service import MarketQueries

pytestmark = pytest.mark.integration
START = parse_instant("2024-01-01")
NOW = datetime(2026, 10, 5, tzinfo=UTC)


@pytest.fixture
def reader(database_settings: DatabaseSettings, admin_engine: Engine) -> Iterator[Engine]:
    engine = create_db_engine(database_settings, DatabaseRole.READ)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(candles.delete())
            conn.execute(ingestion_runs.delete())


@pytest.fixture
def load(reader: Engine, admin_engine: Engine) -> Callable[[Sequence[int]], None]:
    def seed(offsets: Sequence[int]) -> None:
        if not offsets:
            return
        run_id = uuid4()
        with admin_engine.begin() as conn:
            conn.execute(
                ingestion_runs.insert().values(
                    id=run_id,
                    market_id=1,
                    interval_seconds=300,
                    requested_start=START,
                    requested_end=START + (max(offsets) + 1) * INTERVAL,
                    started_at=NOW,
                    finished_at=NOW,
                    status="succeeded",
                    received=len(offsets),
                    inserted=len(offsets),
                )
            )
            conn.execute(
                candles.insert(),
                [
                    {
                        "market_id": 1,
                        "interval_seconds": 300,
                        "opened_at": START + offset * INTERVAL,
                        "open": Decimal(100 + offset * 2),
                        "high": Decimal(105 + offset * 3),
                        "low": Decimal(95 - offset % 2),
                        "close": Decimal(101 + offset * 2),
                        "base_volume": Decimal("0.1") * (offset + 1),
                        "first_ingested_at": NOW,
                        "last_updated_at": NOW,
                        "last_ingestion_run_id": run_id,
                    }
                    for offset in offsets
                ],
            )

    return seed


def queries(reader: Engine, now: datetime = NOW) -> MarketQueries:
    return MarketQueries(reader, ApiSettings(_env_file=None), now=lambda: now)  # type: ignore[call-arg]


def test_complete_summary_matches_hand_calculation(
    reader: Engine, load: Callable[[Sequence[int]], None]
) -> None:
    load([0, 1, 2])
    result = queries(reader).summary(1, START, START + 3 * INTERVAL)
    assert result.coverage.status == "complete" and result.coverage.missing_ranges == []
    assert result.opening_price == 100 and result.closing_price == 105
    assert result.open_to_close_return_percent == Decimal("5.00000000")
    assert result.high == 111 and result.low == 94 and result.base_volume == Decimal("0.6")
    assert result.provenance.ingestion_run_count == 1
    assert result.market.source_code == "coinbase_exchange"


def test_leading_interior_trailing_gaps_hide_full_window_metrics(
    reader: Engine, load: Callable[[Sequence[int]], None]
) -> None:
    load([1, 2, 5])
    result = queries(reader).summary(1, START, START + 7 * INTERVAL)
    assert result.coverage.actual_buckets == 3 and result.coverage.missing_buckets == 4
    assert [(gap.start, gap.end) for gap in result.coverage.missing_ranges] == [
        (START, START + INTERVAL),
        (START + 3 * INTERVAL, START + 5 * INTERVAL),
        (START + 6 * INTERVAL, START + 7 * INTERVAL),
    ]
    assert result.coverage.ratio == Decimal("0.42857143")
    assert result.coverage.status == "incomplete"
    assert all(
        getattr(result, name) is None
        for name in (
            "opening_price",
            "closing_price",
            "open_to_close_return_percent",
            "high",
            "low",
            "base_volume",
        )
    )


def test_no_data_has_one_whole_window_gap(reader: Engine) -> None:
    result = queries(reader).summary(1, START, START + 3 * INTERVAL)
    assert result.coverage.status == "no_data"
    assert result.coverage.missing_ranges[0].missing_buckets == 3
    assert result.coverage.observed_start is None and result.provenance.ingestion_run_count == 0
    assert queries(reader).latest(1).status == "no_data"
    assert queries(reader).candle_page(1, START, START + 3 * INTERVAL).candles == []


def test_missing_range_details_are_bounded_without_losing_counts(
    reader: Engine, load: Callable[[Sequence[int]], None]
) -> None:
    load(list(range(0, 103, 2)))
    coverage = queries(reader).summary(1, START, START + 105 * INTERVAL).coverage
    assert len(coverage.missing_ranges) == 50 and coverage.missing_ranges_truncated
    assert coverage.missing_range_count == 52 and coverage.missing_buckets == 53


@pytest.mark.parametrize("resolution,count", [(900, 3), (3600, 12), (86400, 288)])
def test_derived_candles_use_first_last_extrema_and_exact_volume(
    reader: Engine, load: Callable[[Sequence[int]], None], resolution: int, count: int
) -> None:
    load(list(range(count)))
    page = queries(reader).candle_page(1, START, START + count * INTERVAL, resolution)
    candle = page.candles[0]
    assert len(page.candles) == 1 and candle.status == "complete"
    assert candle.open == 100 and candle.close == 101 + (count - 1) * 2
    assert candle.high == 105 + (count - 1) * 3 and candle.low == 94
    assert candle.base_volume == Decimal(count * (count + 1)) / 20
    assert candle.expected_constituents == count == candle.actual_constituents
    assert candle.provenance.last_ingestion_run_id is None


def test_incomplete_group_has_no_full_bar_prices_and_absent_group_stays_gap(
    reader: Engine, load: Callable[[Sequence[int]], None]
) -> None:
    load([0, 2, 6, 7, 8])
    page = queries(reader).candle_page(1, START, START + 9 * INTERVAL, 900)
    assert [c.opened_at for c in page.candles] == [START, START + 6 * INTERVAL]
    partial = page.candles[0]
    assert partial.status == "incomplete" and partial.actual_constituents == 2
    assert partial.open is None and partial.close is None and partial.base_volume is None
    assert page.coverage.missing_buckets == 4


@pytest.mark.parametrize("resolution", [300, 900])
def test_keyset_pagination_crosses_gaps_without_duplicates(
    reader: Engine, load: Callable[[Sequence[int]], None], resolution: int
) -> None:
    load([0, 1, 2, 6, 7, 8, 9, 10, 11])
    query = queries(reader)
    end = START + 12 * INTERVAL
    expected = query.candle_page(1, START, end, resolution).candles
    gathered = []
    cursor = None
    while True:
        page = query.candle_page(1, START, end, resolution, 2, cursor)
        gathered.extend(page.candles)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert gathered == expected
    assert len({row.opened_at for row in gathered}) == len(gathered)


def test_half_open_ranges_and_offset_normalization(
    reader: Engine, load: Callable[[Sequence[int]], None]
) -> None:
    load([0, 1, 2, 3])
    result = queries(reader).summary(
        1, parse_instant("2024-01-01T01:05:00+01:00"), START + 3 * INTERVAL
    )
    assert (
        result.coverage.actual_buckets == 2
        and result.opening_price == 102
        and result.closing_price == 105
    )


def test_latest_excludes_unsettled_rows_and_reports_age(
    reader: Engine, load: Callable[[Sequence[int]], None]
) -> None:
    load([0, 1, 2, 3, 4])
    result = queries(reader, START + timedelta(minutes=16, microseconds=123456)).latest(1)
    assert result.candle is not None and result.candle.opened_at == START + 2 * INTERVAL
    assert result.age_seconds == Decimal("60.123456") and result.stale is False
    assert result.market.latest_opened_at == START + 2 * INTERVAL


@pytest.mark.parametrize("seconds,stale", [(900, False), (901, True)])
def test_latest_staleness_threshold(
    reader: Engine, load: Callable[[Sequence[int]], None], seconds: int, stale: bool
) -> None:
    load([0])
    result = queries(reader, START + INTERVAL + timedelta(seconds=seconds)).latest(1)
    assert result.stale == stale and result.age_seconds == seconds


def test_readiness_and_actual_reader_snapshot(
    reader: Engine, database_settings: DatabaseSettings
) -> None:
    query = queries(reader)
    assert query.ready()
    with query.snapshot() as conn:
        assert conn.execute(sa.text("SHOW transaction_read_only")).scalar_one() == "on"
        assert conn.execute(sa.text("SHOW transaction_isolation")).scalar_one() == "repeatable read"
        assert (
            conn.execute(sa.text("SELECT current_user")).scalar_one() == database_settings.read_user
        )


def test_summary_snapshot_survives_concurrent_correction(
    reader: Engine,
    admin_engine: Engine,
    load: Callable[[Sequence[int]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load([0, 1, 2])
    query = queries(reader)
    original = query.coverage

    def correct(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        with admin_engine.begin() as conn:
            conn.execute(
                candles.update()
                .where(candles.c.opened_at == START + 2 * INTERVAL)
                .values(close=110)
            )
        return result

    monkeypatch.setattr(query, "coverage", correct)
    assert query.summary(1, START, START + 3 * INTERVAL).closing_price == 105
    assert queries(reader).summary(1, START, START + 3 * INTERVAL).closing_price == 110


def test_http_serialization_uses_decimal_strings_and_provenance(
    reader: Engine, admin_engine: Engine, load: Callable[[Sequence[int]], None]
) -> None:
    load([0])
    exact = Decimal("100.123456789123456789")
    with admin_engine.begin() as conn:
        conn.execute(candles.update().values(open=exact))
    app = create_app(engine=reader, now=lambda: NOW)
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        response = client.get(
            "/v1/markets/1/candles",
            params={
                "start": START.isoformat(),
                "end": (START + INTERVAL).isoformat(),
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["candles"][0]["open"] == str(exact)
        assert result["candles"][0]["provenance"]["last_ingestion_run_id"]
        assert client.get("/v1/markets/99/latest").status_code == 404


def test_invalid_queries_are_rejected(reader: Engine) -> None:
    query = queries(reader)
    with pytest.raises(UnknownMarketError):
        query.summary(99, START, START + INTERVAL)
    with pytest.raises(QueryValidationError):
        query.candle_page(1, START, START + INTERVAL, limit=501)
    with pytest.raises(QueryValidationError):
        query.candle_page(1, START, START + INTERVAL, cursor="!")
