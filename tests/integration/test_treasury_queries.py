"""Reader-role Treasury evidence, exact spread units, pagination, and snapshot consistency."""

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from market_intelligence.api import create_app
from market_intelligence.config import AgentSettings, DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.tables import treasury_yields
from market_intelligence.db.treasury_store import TreasuryStore
from market_intelligence.queries.models import QueryValidationError
from market_intelligence.treasury.models import TreasuryTenor
from market_intelligence.treasury.queries import TreasuryQueries
from market_intelligence.treasury.service import ingest_treasury
from tests.integration.test_treasury_ingestion import END, NOW, START, source_with
from tests.unit.test_treasury import entry, feed

pytestmark = pytest.mark.integration


@pytest.fixture
def treasury_queries(database_settings: DatabaseSettings) -> Iterator[TreasuryQueries]:
    engine = create_db_engine(database_settings, DatabaseRole.READ)
    try:
        yield TreasuryQueries(engine, now=lambda: NOW)
    finally:
        engine.dispose()


def load(store: TreasuryStore) -> None:
    with source_with() as source:
        ingest_treasury(source, store, START, END, now=lambda: NOW)


def test_curve_has_exact_native_units_negative_spread_and_provenance(
    treasury_queries: TreasuryQueries,
    treasury_store: TreasuryStore,
) -> None:
    load(treasury_store)
    result = treasury_queries.curve(date(2024, 1, 2))
    curve = result.curve
    assert result.source_code == "us_treasury" and result.yield_unit == "percent"
    assert curve.status == "stored" and curve.stored_rates == 14 and curve.available_rates == 2
    assert [rate.tenor for rate in curve.rates] == list(TreasuryTenor)
    assert curve.spread.percentage_points == Decimal("-0.125")
    assert curve.spread.basis_points == Decimal("-12.5")
    assert curve.spread.status == "available" and curve.spread.missing_inputs == []
    assert curve.latest_month_read is not None and curve.latest_month_read.received_dates == 2
    rate = next(rate for rate in curve.rates if rate.tenor == TreasuryTenor.TWO_YEARS)
    assert rate.provenance is not None and rate.provenance.last_updated_at == NOW
    payload = result.model_dump(mode="json")
    assert isinstance(payload["curve"]["spread"]["basis_points"], str)
    assert isinstance(payload["curve"]["rates"][7]["yield_percent"], str)


def test_no_source_date_returns_no_data_without_holiday_inference(
    treasury_queries: TreasuryQueries,
    treasury_store: TreasuryStore,
) -> None:
    load(treasury_store)
    result = treasury_queries.curve(date(2024, 1, 1))
    assert result.curve.status == "no_data" and result.curve.stored_rates == 0
    assert all(rate.missing_reason == "not_stored" for rate in result.curve.rates)
    assert result.curve.spread.status == "unavailable"
    empty = treasury_queries.curve_page(date(2023, 12, 1), START)
    assert empty.curves == [] and empty.coverage.status == "no_data"
    assert empty.coverage.publication_calendar_completeness == "not_established"


def test_missing_benchmarks_are_unavailable_and_zero_is_a_value(
    treasury_queries: TreasuryQueries,
    treasury_store: TreasuryStore,
) -> None:
    payload = feed(
        entry("2024-01-02", '<d:BC_2YEAR m:null="true"/>')
        + entry("2024-01-03", "<d:BC_2YEAR>0</d:BC_2YEAR><d:BC_10YEAR>0.25</d:BC_10YEAR>")
    )
    with source_with(lambda request: httpx.Response(200, content=payload)) as source:
        ingest_treasury(source, treasury_store, START, END, now=lambda: NOW)
    missing = treasury_queries.curve(date(2024, 1, 2)).curve.spread
    assert missing.percentage_points is None and missing.basis_points is None
    assert missing.missing_inputs == [TreasuryTenor.TWO_YEARS, TreasuryTenor.TEN_YEARS]
    zero = treasury_queries.curve(date(2024, 1, 3)).curve.spread
    assert zero.status == "available" and zero.basis_points == Decimal(25)


def test_date_only_entry_is_stored_with_no_available_yields_or_spread(
    treasury_queries: TreasuryQueries,
    treasury_store: TreasuryStore,
) -> None:
    with source_with(lambda _: httpx.Response(200, content=feed(entry()))) as source:
        report = ingest_treasury(source, treasury_store, START, END, now=lambda: NOW)[0]
    assert report.received_dates == 1 and report.field_absent == 14
    curve = treasury_queries.curve(date(2024, 1, 2)).curve
    assert curve.status == "stored" and curve.stored_rates == 14 and curve.available_rates == 0
    assert all(
        rate.yield_percent is None and rate.missing_reason == "field_absent" for rate in curve.rates
    )
    assert curve.spread.status == "unavailable" and curve.spread.percentage_points is None
    page = treasury_queries.curve_page(START, END)
    assert page.coverage.observed_dates == 1 and page.coverage.available_rates == 0
    assert page.coverage.field_absent == 14
    assert page.coverage.publication_calendar_completeness == "not_established"


def test_half_open_pagination_coverage_describes_whole_window(
    treasury_queries: TreasuryQueries,
    treasury_store: TreasuryStore,
) -> None:
    load(treasury_store)
    first = treasury_queries.curve_page(START, END, limit=1)
    assert [curve.observed_on for curve in first.curves] == [date(2024, 1, 2)]
    assert first.next_cursor and first.coverage.observed_dates == 2
    assert first.coverage.stored_rates == 28 and first.coverage.available_rates == 4
    assert first.coverage.field_absent == 24 and first.coverage.source_null == 0
    second = treasury_queries.curve_page(START, END, limit=1, cursor=first.next_cursor)
    assert [curve.observed_on for curve in second.curves] == [date(2024, 1, 3)]
    assert second.next_cursor is None and second.coverage == first.coverage
    narrow = treasury_queries.curve_page(date(2024, 1, 2), date(2024, 1, 3))
    assert len(narrow.curves) == 1 and narrow.coverage.observed_dates == 1
    with pytest.raises(QueryValidationError, match="cursor"):
        treasury_queries.curve_page(START, date(2024, 2, 2), cursor=first.next_cursor)


def test_partial_stored_curve_exposes_missing_row(
    treasury_queries: TreasuryQueries,
    treasury_store: TreasuryStore,
    admin_engine: Engine,
) -> None:
    load(treasury_store)
    with admin_engine.begin() as conn:
        conn.execute(
            treasury_yields.delete().where(
                treasury_yields.c.observed_on == date(2024, 1, 2), treasury_yields.c.tenor == "2Y"
            )
        )
    curve = treasury_queries.curve(date(2024, 1, 2)).curve
    assert curve.status == "incomplete_stored_curve" and curve.stored_rates == 13
    assert curve.spread.missing_inputs == [TreasuryTenor.TWO_YEARS]
    assert next(rate for rate in curve.rates if rate.tenor == "2Y").missing_reason == "not_stored"


def test_omitted_month_read_does_not_relabel_unchanged_fact_provenance(
    treasury_queries: TreasuryQueries,
    treasury_store: TreasuryStore,
) -> None:
    load(treasury_store)
    before = treasury_queries.curve(date(2024, 1, 2)).curve
    with source_with(lambda request: httpx.Response(200, content=feed())) as source:
        ingest_treasury(source, treasury_store, START, END, now=lambda: NOW + timedelta(hours=1))
    curve = treasury_queries.curve(date(2024, 1, 2)).curve
    assert curve.rates == before.rates and curve.latest_month_read is not None
    assert (
        curve.latest_month_read.retained_dates == 2 and curve.latest_month_read.received_dates == 0
    )


def test_snapshot_uses_reader_and_remains_consistent_through_correction(
    treasury_queries: TreasuryQueries,
    treasury_store: TreasuryStore,
    admin_engine: Engine,
    database_settings: DatabaseSettings,
) -> None:
    load(treasury_store)
    with treasury_queries.snapshot() as conn:
        assert (
            conn.execute(sa.text("SELECT current_user")).scalar_one()
            == database_settings.credentials(DatabaseRole.READ)[0]
        )
        assert conn.execute(sa.text("SHOW transaction_read_only")).scalar_one() == "on"
        assert conn.execute(sa.text("SHOW transaction_isolation")).scalar_one() == "repeatable read"
    corrected = False

    def correct_after_summary(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        nonlocal corrected
        if not corrected and "count(DISTINCT" in statement and "treasury_yields" in statement:
            corrected = True
            with admin_engine.begin() as writer:
                writer.execute(
                    treasury_yields.update()
                    .where(treasury_yields.c.tenor == "2Y")
                    .values(yield_percent=8)
                )

    sa.event.listen(treasury_queries.engine, "after_cursor_execute", correct_after_summary)
    try:
        page = treasury_queries.curve_page(START, END)
    finally:
        sa.event.remove(treasury_queries.engine, "after_cursor_execute", correct_after_summary)
    assert corrected and page.curves[0].spread.percentage_points == Decimal("-0.125")
    assert treasury_queries.curve(date(2024, 1, 2)).curve.spread.percentage_points == Decimal(
        "-3.875"
    )


def test_treasury_http_routes_share_queries_and_document_pagination(
    treasury_queries: TreasuryQueries,
    treasury_store: TreasuryStore,
) -> None:
    load(treasury_store)
    with TestClient(
        create_app(
            engine=treasury_queries.engine,
            now=lambda: NOW,
            agent_settings=AgentSettings(_env_file=None),  # type: ignore[call-arg]
        )
    ) as client:
        one = client.get("/v1/treasury/curve", params={"observed_on": "2024-01-02"})
        assert one.status_code == 200 and one.json()["curve"]["spread"]["status"] == "available"
        first = client.get(
            "/v1/treasury/curves", params={"start": "2024-01-01", "end": "2024-02-01", "limit": 1}
        )
        assert first.status_code == 200 and first.json()["next_cursor"]
        second = client.get(
            "/v1/treasury/curves",
            params={
                "start": "2024-01-01",
                "end": "2024-02-01",
                "limit": 1,
                "cursor": first.json()["next_cursor"],
            },
        )
        assert (
            second.status_code == 200 and second.json()["curves"][0]["observed_on"] == "2024-01-03"
        )
        assert client.get("/health/ready").status_code == 200
        assert "/v1/treasury/curves" in client.get("/openapi.json").json()["paths"]
        for bad_date in ("20240102", "2024-01-02T00:00:00Z", "2024-02-30", "2027-01-01"):
            assert (
                client.get("/v1/treasury/curve", params={"observed_on": bad_date}).status_code
                == 422
            )
        assert (
            client.get(
                "/v1/treasury/curves",
                params={"start": "2024-01-01", "end": "2024-02-01", "limit": 101},
            ).status_code
            == 422
        )
