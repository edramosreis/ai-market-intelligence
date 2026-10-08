from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import Connection
from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError

from market_intelligence.cli import migration_config
from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.macro_tables import (
    macro_current,
    macro_ingestion_runs,
    macro_observed_versions,
    macro_series,
    macro_version_footnotes,
)
from market_intelligence.db.tables import (
    candles,
    funding_events,
    funding_ingestion_runs,
    open_interest_snapshots,
    treasury_yields,
)
from market_intelligence.macro.models import CATALOG, MacroSeries
from tests.integration.test_hyperliquid_schema import funding_values, oi_values
from tests.integration.test_schema import candle_values
from tests.integration.test_treasury_schema import treasury_yield_values

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def run_values() -> dict[str, Any]:
    return dict(
        id=uuid4(),
        source_code="bls",
        requested_start=date(2024, 1, 1),
        requested_end=date(2025, 1, 1),
        started_at=NOW,
        status="running",
    )


def version_values(conn: Connection, *, fed: bool = False) -> dict[str, Any]:
    run = run_values()
    if fed:
        run["source_code"] = "federal_reserve_board"
    conn.execute(macro_ingestion_runs.insert().values(**run))
    return dict(
        source_code=run["source_code"],
        series_id="RIFSPFF_N.M" if fed else "CUSR0000SA0",
        observation_month=date(2024, 1, 1),
        version_number=1,
        native_period="2024-01-31" if fed else "2024-M01",
        value=Decimal("1.123456789123456789"),
        missing_reason=None,
        materialized_at=NOW,
        ingestion_run_id=run["id"],
    )


def test_catalog_matches_reviewed_native_models(connection: Connection) -> None:
    rows = connection.execute(sa.select(macro_series)).mappings().all()
    assert len(rows) == 3
    for row in rows:
        native = CATALOG[MacroSeries(row["series_id"])]
        assert (
            row["source_code"],
            row["title"],
            row["unit"],
            row["seasonal_adjustment"],
            row["earliest_month"],
        ) == (
            native.provider,
            native.title,
            native.unit,
            native.seasonal_adjustment,
            native.earliest_month,
        )
    config = migration_config()
    config.attributes["connection"] = connection
    command.check(config)


def test_current_pointer_and_ordered_footnotes_round_trip(connection: Connection) -> None:
    values = version_values(connection)
    connection.execute(macro_observed_versions.insert().values(**values))
    key = {
        name: values[name]
        for name in ("source_code", "series_id", "observation_month", "version_number")
    }
    connection.execute(macro_current.insert().values(**key))
    connection.execute(
        macro_version_footnotes.insert().values(**key, ordinal=0, code="X", text="Synthetic note")
    )
    row = (
        connection.execute(
            sa.select(macro_observed_versions).select_from(
                macro_current.join(macro_observed_versions)
            )
        )
        .mappings()
        .one()
    )
    assert row["value"] == values["value"] and row["materialized_at"] == NOW
    assert (
        connection.execute(sa.select(macro_version_footnotes.c.text)).scalar_one()
        == "Synthetic note"
    )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(macro_current.update().values(version_number=2))
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(macro_current.update().values(observation_month=date(2024, 2, 1)))


@pytest.mark.parametrize(
    "changes",
    [
        {"observation_month": date(2024, 1, 2)},
        {"observation_month": date(1946, 12, 1)},
        {"version_number": 0},
        {"native_period": "2024-01-31"},
        {"series_id": "Unknown"},
        {"source_code": "federal_reserve_board"},
        {"value": None},
        {"value": Decimal("0")},
        {"value": Decimal("NaN")},
        {"value": Decimal("Infinity")},
        {"value": Decimal("1e20")},
        {"value": Decimal("1"), "missing_reason": "source_dash"},
        {"value": None, "missing_reason": "not_stored"},
        {"ingestion_run_id": uuid4()},
    ],
)
def test_invalid_native_versions_fail(connection: Connection, changes: dict[str, Any]) -> None:
    values = version_values(connection) | changes
    with pytest.raises((DataError, IntegrityError)), connection.begin_nested():
        connection.execute(macro_observed_versions.insert().values(**values))


@pytest.mark.parametrize(
    "changes",
    [
        {"requested_start": date(2024, 1, 2)},
        {"requested_end": date(2024, 1, 1)},
        {"requested_start": date(2014, 12, 1)},
        {"source_code": "coinbase_exchange"},
        {"status": "unknown"},
        {"status": "succeeded"},
        {"received": 1},
        {"retained": 1},
        {"annual_average_count": 1},
        {"source_messages": ["Unexpected running metadata"]},
        {"status": "failed", "finished_at": NOW, "error_code": None},
        {"status": "failed", "finished_at": NOW, "error_code": "Private error"},
    ],
)
def test_invalid_receipt_audits_fail(connection: Connection, changes: dict[str, Any]) -> None:
    with pytest.raises((DataError, IntegrityError)), connection.begin_nested():
        connection.execute(macro_ingestion_runs.insert().values(**(run_values() | changes)))


def test_explicit_dash_zero_and_fed_native_month_end(connection: Connection) -> None:
    values = version_values(connection)
    values.update(series_id="LNS14000000", value=None, missing_reason="source_dash")
    connection.execute(macro_observed_versions.insert().values(**values))
    values.update(
        observation_month=date(2024, 2, 1),
        native_period="2024-M02",
        value=Decimal("0"),
        missing_reason=None,
    )
    connection.execute(macro_observed_versions.insert().values(**values))
    fed = version_values(connection, fed=True)
    connection.execute(macro_observed_versions.insert().values(**fed))
    changes: list[dict[str, Any]] = [
        {"value": None, "missing_reason": "source_dash"},
        {"value": Decimal("-9999")},
        {"native_period": "2024-01-30"},
    ]
    for change in changes:
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                macro_observed_versions.insert().values(**(fed | change | {"version_number": 2}))
            )


@pytest.mark.parametrize(
    "changes", [{"ordinal": 20}, {"ordinal": -1}, {"code": ""}, {"text": ""}, {"version_number": 2}]
)
def test_invalid_immutable_footnotes_fail(connection: Connection, changes: dict[str, Any]) -> None:
    values = version_values(connection)
    connection.execute(macro_observed_versions.insert().values(**values))
    key = {
        name: values[name]
        for name in ("source_code", "series_id", "observation_month", "version_number")
    }
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            macro_version_footnotes.insert().values(
                **(key | {"ordinal": 0, "code": "X", "text": "Synthetic"} | changes)
            )
        )


def test_upgrade_preserves_all_existing_source_facts(connection: Connection) -> None:
    funding = funding_values(connection)
    funding["event_at"] += timedelta(days=1)
    funding["settlement_hour"] += timedelta(days=1)
    connection.execute(
        funding_ingestion_runs.update()
        .where(funding_ingestion_runs.c.id == funding["last_ingestion_run_id"])
        .values(
            requested_start=funding["settlement_hour"],
            requested_end=funding["settlement_hour"] + timedelta(hours=1),
        )
    )
    cases = [
        (candles, candle_values(connection)),
        (treasury_yields, treasury_yield_values(connection)),
        (funding_events, funding),
        (open_interest_snapshots, oi_values(connection)),
    ]
    for table, values in cases:
        connection.execute(table.insert().values(**values))

    def snapshot() -> list[list[dict[str, Any]]]:
        return [
            [
                dict(row)
                for row in connection.execute(
                    sa.select(table).order_by(*table.primary_key.columns)
                ).mappings()
            ]
            for table, _ in cases
        ]

    before = snapshot()
    config = migration_config()
    config.attributes["connection"] = connection
    command.downgrade(config, "0003")
    assert "macro_current" not in sa.inspect(connection).get_table_names()
    command.upgrade(config, "head")
    assert snapshot() == before
    command.check(config)


@pytest.mark.parametrize("role", [DatabaseRole.READ, DatabaseRole.INGEST])
def test_macro_least_privilege_and_immutable_version_grants(
    database_settings: DatabaseSettings, role: DatabaseRole
) -> None:
    engine = create_db_engine(database_settings, role)
    try:
        with engine.connect() as conn, conn.begin():
            for table in (
                macro_series,
                macro_ingestion_runs,
                macro_observed_versions,
                macro_version_footnotes,
                macro_current,
            ):
                conn.execute(sa.select(table)).all()
                with pytest.raises(ProgrammingError), conn.begin_nested():
                    conn.execute(table.delete())
            for table in (macro_series, macro_observed_versions, macro_version_footnotes):
                with pytest.raises(ProgrammingError), conn.begin_nested():
                    conn.execute(
                        table.update().values(**{next(iter(table.c)).name: next(iter(table.c))})
                    )
            if role == DatabaseRole.READ:
                for table in (
                    macro_ingestion_runs,
                    macro_current,
                    macro_observed_versions,
                    macro_version_footnotes,
                ):
                    with pytest.raises(ProgrammingError), conn.begin_nested():
                        conn.execute(sa.text(f"INSERT INTO {table.name} DEFAULT VALUES"))
            else:
                values = version_values(conn)
                conn.execute(macro_observed_versions.insert().values(**values))
                key = {
                    name: values[name]
                    for name in ("source_code", "series_id", "observation_month", "version_number")
                }
                conn.execute(macro_current.insert().values(**key))
                conn.execute(macro_current.update().values(version_number=1))
                conn.execute(
                    macro_version_footnotes.insert().values(
                        **key, ordinal=0, code="X", text="Synthetic"
                    )
                )
                conn.execute(
                    macro_ingestion_runs.update().values(
                        status="failed", finished_at=NOW, error_code="http_error"
                    )
                )
    finally:
        engine.dispose()
