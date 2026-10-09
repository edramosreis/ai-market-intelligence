"""Actual reader snapshots, native evidence, observed versions and HTTP parity."""

import calendar
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.api import create_app
from market_intelligence.config import AgentSettings, ApiSettings, DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.macro_store import MacroStore
from market_intelligence.db.macro_tables import (
    macro_current,
    macro_ingestion_runs,
    macro_observed_versions,
    macro_version_footnotes,
)
from market_intelligence.macro.models import (
    BLS_NOTICE,
    Footnote,
    MacroProvider,
    MacroSeries,
    MonthlyObservation,
    MonthlyWindow,
    ProviderRead,
)
from market_intelligence.macro.queries import MacroQueries
from market_intelligence.queries.models import QueryValidationError

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
START, END = date(2024, 1, 1), date(2024, 5, 1)
WINDOW = MonthlyWindow(START, END)


@pytest.fixture
def macro_queries(database_settings: DatabaseSettings) -> Iterator[MacroQueries]:
    engine = create_db_engine(database_settings, DatabaseRole.READ)
    try:
        yield MacroQueries(
            engine,
            ApiSettings(_env_file=None),  # type: ignore[call-arg]
            now=lambda: NOW + timedelta(hours=1),
        )
    finally:
        engine.dispose()


def observation(
    series: MacroSeries = MacroSeries.CPI,
    month: date = START,
    value: str | None = "100.125",
    footnotes: tuple[Footnote, ...] = (),
) -> MonthlyObservation:
    native = (
        month.replace(day=calendar.monthrange(month.year, month.month)[1]).isoformat()
        if series == MacroSeries.FED_FUNDS
        else month.strftime("%Y-M%m")
    )
    return MonthlyObservation(
        series,
        month,
        native,
        Decimal(value) if value is not None else None,
        "source_dash" if value is None else None,
        footnotes,
    )


def load(
    store: MacroStore,
    rows: tuple[MonthlyObservation, ...],
    seconds: int = 0,
    provider: MacroProvider = MacroProvider.BLS,
    window: MonthlyWindow = WINDOW,
) -> None:
    started = NOW + timedelta(seconds=seconds)
    cpi_months = [row.month for row in rows if row.series == MacroSeries.CPI]
    read = ProviderRead(
        window,
        tuple(sorted(rows, key=lambda row: (row.series.value, row.month))),
        started + timedelta(seconds=1),
        started + timedelta(seconds=2),
        source_messages=("Synthetic message",) if provider == MacroProvider.BLS else (),
        latest_hints=((MacroSeries.CPI, max(cpi_months)),) if cpi_months else (),
        prepared_text="2026-10-09T08:30:00" if provider == MacroProvider.FED else None,
        source_annotations=(("Unit", "Percent"),) if provider == MacroProvider.FED else (),
    )
    run = store.start_run(provider, window, started)
    store.persist(run, provider, read, started + timedelta(seconds=3))


def counts(engine: Engine) -> list[int]:
    with engine.connect() as conn:
        return [
            conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
            for table in (
                macro_current,
                macro_observed_versions,
                macro_version_footnotes,
                macro_ingestion_runs,
            )
        ]


def test_fixed_catalog_units_adjustment_and_actual_local_counts(
    macro_store: MacroStore, macro_queries: MacroQueries
) -> None:
    load(macro_store, (observation(),))
    data = {row.series_id: row for row in macro_queries.list_series()}
    assert set(data) == set(MacroSeries)
    cpi = data[MacroSeries.CPI]
    assert cpi.unit == "index_1982_84_100" and cpi.seasonal_adjustment == "seasonally_adjusted"
    assert cpi.earliest_native_month == date(1947, 1, 1) and cpi.stored_months == 1
    assert (
        cpi.first_stored_month == cpi.last_stored_month == START and cpi.source_notice == BLS_NOTICE
    )
    fed = data[MacroSeries.FED_FUNDS]
    assert fed.unit == "percent_per_annum" and fed.seasonal_adjustment == "not_seasonally_adjusted"
    assert fed.source_code == "federal_reserve_board" and fed.stored_months == 0


def test_month_grid_dash_absence_decimal_footnotes_and_receipt_origin(
    macro_store: MacroStore, macro_queries: MacroQueries
) -> None:
    load(
        macro_store,
        (
            observation(),
            observation(
                month=date(2024, 3, 1),
                value=None,
                footnotes=(Footnote("X", "Synthetic unavailable"),),
            ),
        ),
    )
    page = macro_queries.observations(MacroSeries.CPI, START, END)
    coverage = page.coverage
    assert (
        coverage.expected_months,
        coverage.stored_months,
        coverage.available_values,
        coverage.source_missing_values,
        coverage.not_stored_months,
    ) == (4, 2, 1, 1, 2)
    assert coverage.status == "incomplete"
    assert [(gap.start, gap.end) for gap in coverage.missing_ranges] == [
        (date(2024, 2, 1), date(2024, 3, 1)),
        (date(2024, 4, 1), END),
    ]
    first, missing = page.observations
    assert first.value == Decimal("100.125") and first.native_period == "2024-M01"
    assert (
        missing.value is None
        and missing.missing_reason == "source_dash"
        and missing.status == "source_missing"
    )
    assert missing.footnotes[0].text == "Synthetic unavailable"
    assert (
        len(page.receipts) == 1 and first.provenance.content_receipt_id == page.receipts[0].run_id
    )
    receipt = page.receipts[0]
    assert receipt.access_date == NOW.date() and receipt.source_messages == ["Synthetic message"]
    assert receipt.latest_hints == [(MacroSeries.CPI, date(2024, 3, 1))]
    assert receipt.meaning == "receipt_that_materialized_version_content"
    assert (
        page.historical_release_vintages
        == receipt.per_observation_publication_time
        == "not_established"
    )
    assert isinstance(page.model_dump(mode="json")["observations"][0]["value"], str)


def test_keyset_pages_keep_whole_window_coverage(
    macro_store: MacroStore, macro_queries: MacroQueries
) -> None:
    load(macro_store, tuple(observation(month=date(2024, month, 1)) for month in (1, 2, 4)))
    first = macro_queries.observations(MacroSeries.CPI, START, END, limit=2)
    second = macro_queries.observations(
        MacroSeries.CPI, START, END, limit=2, cursor=first.next_cursor
    )
    assert [row.month for row in first.observations + second.observations] == [
        START,
        date(2024, 2, 1),
        date(2024, 4, 1),
    ]
    assert first.coverage == second.coverage and first.coverage.stored_months == 3
    assert first.next_cursor and second.next_cursor is None
    with pytest.raises(QueryValidationError):
        macro_queries.observations(MacroSeries.UNEMPLOYMENT, START, END, cursor=first.next_cursor)
    with pytest.raises(QueryValidationError):
        macro_queries.observations(
            MacroSeries.CPI, START, date(2024, 4, 1), cursor=first.next_cursor
        )


def test_no_data_has_empty_observations_full_gap_and_no_fabricated_provenance(
    macro_queries: MacroQueries,
) -> None:
    page = macro_queries.observations(MacroSeries.CPI, START, END)
    assert page.observations == [] and page.receipts == [] and page.next_cursor is None
    assert page.coverage.status == "no_data" and page.coverage.not_stored_months == 4
    assert len(page.coverage.missing_ranges) == 1
    latest = macro_queries.latest(MacroSeries.CPI)
    assert (
        latest.status == "no_data"
        and latest.observation is None
        and latest.months_behind_latest_completed is None
    )
    history = macro_queries.observed_versions(MacroSeries.CPI, START)
    assert history.versions == [] and history.receipts == [] and history.observed_versions == 0
    assert history.current_version_number is None


def test_latest_stored_month_keeps_unavailable_value_without_substitution(
    macro_store: MacroStore, macro_queries: MacroQueries
) -> None:
    load(macro_store, (observation(), observation(month=date(2024, 3, 1), value=None)))
    latest = macro_queries.latest(MacroSeries.CPI)
    assert latest.status == "stored" and latest.observation is not None
    assert latest.observation.month == date(2024, 3, 1) and latest.observation.value is None
    assert (
        latest.latest_completed_month == date(2026, 9, 1)
        and latest.months_behind_latest_completed == 30
    )
    assert latest.publication_delay == "not_established"


def test_fed_native_month_end_zero_rate_and_prepared_metadata(
    macro_store: MacroStore, macro_queries: MacroQueries
) -> None:
    load(
        macro_store,
        (observation(MacroSeries.FED_FUNDS, date(2024, 2, 1), "0"),),
        provider=MacroProvider.FED,
    )
    page = macro_queries.observations(MacroSeries.FED_FUNDS, START, END)
    row = page.observations[0]
    assert row.month == date(2024, 2, 1) and row.native_period == "2024-02-29"
    assert row.value == 0 and row.status == "available" and page.series.unit == "percent_per_annum"
    receipt = page.receipts[0]
    assert receipt.prepared_text == "2026-10-09T08:30:00" and receipt.source_annotations == [
        ("Unit", "Percent")
    ]
    assert receipt.per_observation_publication_time == "not_established" and row.footnotes == []


def test_latest_and_catalog_exclude_uncompleted_stored_months(
    macro_store: MacroStore, macro_queries: MacroQueries
) -> None:
    load(macro_store, (observation(),))
    load(
        macro_store,
        (observation(month=date(2026, 10, 1), value="999"),),
        seconds=10,
        window=MonthlyWindow(date(2026, 10, 1), date(2026, 11, 1)),
    )
    latest = macro_queries.latest(MacroSeries.CPI)
    assert latest.observation is not None and latest.observation.month == START
    assert latest.series.stored_months == 1 and latest.series.last_stored_month == START


def test_observed_versions_distinguish_source_missing_then_locally_available(
    macro_store: MacroStore, macro_queries: MacroQueries
) -> None:
    load(macro_store, (observation(value=None, footnotes=(Footnote("X", "Synthetic missing"),)),))
    load(macro_store, (observation(),), seconds=10)
    page = macro_queries.observed_versions(MacroSeries.CPI, START)
    assert [row.status for row in page.versions] == ["source_missing", "available"]
    assert [row.value for row in page.versions] == [None, Decimal("100.125")]
    assert page.versions[0].footnotes[0].code == "X" and page.versions[1].footnotes == []
    assert page.current_version_number == 2 and len(page.receipts) == 2


def test_replay_and_omission_do_not_claim_a_new_content_receipt(
    macro_store: MacroStore, macro_queries: MacroQueries
) -> None:
    load(macro_store, (observation(), observation(month=date(2024, 2, 1))))
    before = macro_queries.observations(MacroSeries.CPI, START, END)
    load(macro_store, (observation(),), seconds=10)
    after = macro_queries.observations(MacroSeries.CPI, START, END)
    assert after.observations == before.observations and after.receipts == before.receipts
    assert counts(macro_store.engine)[-1] == 2


def test_changed_values_and_footnotes_have_paginated_local_versions(
    macro_store: MacroStore, macro_queries: MacroQueries
) -> None:
    load(macro_store, (observation(footnotes=(Footnote("P", "Synthetic first"),)),))
    load(
        macro_store,
        (observation(value="101", footnotes=(Footnote("R", "Synthetic corrected"),)),),
        seconds=10,
    )
    load(
        macro_store,
        (observation(value="101", footnotes=(Footnote("R", "Synthetic revised note"),)),),
        seconds=20,
    )
    page = macro_queries.observed_versions(MacroSeries.CPI, START, limit=2)
    second = macro_queries.observed_versions(
        MacroSeries.CPI, START, limit=2, cursor=page.next_cursor
    )
    assert page.observed_versions == second.observed_versions == page.current_version_number == 3
    assert [row.version_number for row in page.versions + second.versions] == [1, 2, 3]
    assert page.versions[0].value == Decimal("100.125") and page.versions[1].value == 101
    assert second.versions[0].footnotes[0].text == "Synthetic revised note"
    assert (
        second.versions[0].provenance.first_materialized_at
        == page.versions[0].provenance.materialized_at
    )
    current = macro_queries.observations(MacroSeries.CPI, START, END).observations[0]
    assert current == second.versions[0] and len(page.receipts) == 2 and len(second.receipts) == 1
    assert second.next_cursor is None
    with pytest.raises(QueryValidationError):
        macro_queries.observed_versions(MacroSeries.CPI, date(2024, 2, 1), cursor=page.next_cursor)


def test_readonly_snapshot_and_queries_do_not_write(
    macro_store: MacroStore, macro_queries: MacroQueries
) -> None:
    load(macro_store, (observation(),))
    before = counts(macro_store.engine)
    with macro_queries.snapshot() as conn:
        assert conn.execute(sa.text("SHOW transaction_read_only")).scalar_one() == "on"
        assert conn.execute(sa.text("SHOW transaction_isolation")).scalar_one() == "repeatable read"
    macro_queries.list_series()
    macro_queries.latest(MacroSeries.CPI)
    macro_queries.observations(MacroSeries.CPI, START, END)
    macro_queries.observed_versions(MacroSeries.CPI, START)
    assert counts(macro_store.engine) == before
    with pytest.raises(SQLAlchemyError), macro_queries.snapshot() as conn:
        conn.execute(macro_current.delete())


@pytest.mark.parametrize("method", ["observations", "observed_versions"])
def test_concurrent_correction_keeps_catalog_coverage_content_notes_and_receipts_consistent(
    macro_store: MacroStore, macro_queries: MacroQueries, method: str
) -> None:
    load(macro_store, (observation(footnotes=(Footnote("P", "Synthetic old"),)),))
    changed = False

    def correct(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        nonlocal changed
        if not changed and "FROM macro_series" in statement:
            changed = True
            load(
                macro_store,
                (
                    observation(value="101", footnotes=(Footnote("R", "Synthetic new"),)),
                    observation(month=date(2024, 2, 1)),
                ),
                seconds=10,
            )

    sa.event.listen(macro_queries.engine, "after_cursor_execute", correct)
    try:
        result = (
            macro_queries.observations(MacroSeries.CPI, START, END)
            if method == "observations"
            else macro_queries.observed_versions(MacroSeries.CPI, START)
        )
    finally:
        sa.event.remove(macro_queries.engine, "after_cursor_execute", correct)
    evidence = result.observations if hasattr(result, "observations") else result.versions
    assert changed and result.series.stored_months == 1 and len(evidence) == 1
    assert evidence[0].version_number == 1 and evidence[0].value == Decimal("100.125")
    assert evidence[0].footnotes[0].text == "Synthetic old"
    assert evidence[0].provenance.content_receipt_id == result.receipts[0].run_id
    fresh = macro_queries.observations(MacroSeries.CPI, START, END)
    assert fresh.series.stored_months == fresh.coverage.stored_months == 2
    assert fresh.observations[0].value == 101 and fresh.observations[0].version_number == 2


def test_http_routes_match_real_reader_evidence_and_preserve_all_macro_counts(
    macro_store: MacroStore, macro_queries: MacroQueries
) -> None:
    load(macro_store, (observation(), observation(month=date(2024, 2, 1), value=None)))
    before = counts(macro_store.engine)
    app = create_app(
        engine=macro_queries.engine,
        api_settings=macro_queries.settings,
        agent_settings=AgentSettings(_env_file=None),  # type: ignore[call-arg]
        now=macro_queries.now,
    )
    observation_path = (
        "/v1/macro/series/CUSR0000SA0/observations?start=2024-01-01&end=2024-05-01&limit=1"
    )
    expected = {
        observation_path: macro_queries.observations(MacroSeries.CPI, START, END, 1),
        "/v1/macro/series/CUSR0000SA0/latest": macro_queries.latest(MacroSeries.CPI),
        "/v1/macro/series/CUSR0000SA0/versions?month=2024-01-01": macro_queries.observed_versions(
            MacroSeries.CPI, START
        ),
    }
    with TestClient(app) as client:
        assert client.get("/v1/macro/series").json() == [
            row.model_dump(mode="json") for row in macro_queries.list_series()
        ]
        for path, evidence in expected.items():
            response = client.get(path)
            assert response.status_code == 200 and response.json() == evidence.model_dump(
                mode="json"
            )
        page = client.get(next(iter(expected))).json()
        second = client.get(
            "/v1/macro/series/CUSR0000SA0/observations",
            params={
                "start": "2024-01-01",
                "end": "2024-05-01",
                "limit": 1,
                "cursor": page["next_cursor"],
            },
        ).json()
        assert second["coverage"] == page["coverage"] and second["observations"][0]["value"] is None
    assert counts(macro_store.engine) == before
