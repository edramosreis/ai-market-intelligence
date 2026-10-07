"""Treasury date/rate constraints and migration compatibility on isolated PostgreSQL."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import Connection
from sqlalchemy.exc import DataError, IntegrityError

from market_intelligence.cli import migration_config
from market_intelligence.db.tables import candles, treasury_ingestion_runs, treasury_yields
from tests.integration.test_schema import candle_values

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 6, tzinfo=UTC)


def treasury_run_values() -> dict[str, Any]:
    return {
        "id": uuid4(),
        "source_code": "us_treasury",
        "dataset_code": "daily_nominal_par_yield_curve",
        "requested_start": date(2024, 1, 1),
        "requested_end": date(2024, 2, 1),
        "started_at": NOW,
        "status": "running",
    }


def treasury_yield_values(conn: Connection) -> dict[str, Any]:
    run = treasury_run_values()
    conn.execute(treasury_ingestion_runs.insert().values(**run))
    return {
        "source_code": run["source_code"],
        "dataset_code": run["dataset_code"],
        "observed_on": date(2024, 1, 2),
        "tenor": "2Y",
        "yield_percent": Decimal("4.250000000000000001"),
        "missing_reason": None,
        "first_ingested_at": NOW,
        "last_updated_at": NOW,
        "last_ingestion_run_id": run["id"],
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"yield_percent": Decimal(-1)},
        {"yield_percent": Decimal("NaN")},
        {"yield_percent": Decimal("Infinity")},
        {"yield_percent": Decimal("1e20")},
        {"yield_percent": None},
        {"missing_reason": "source_null"},
        {"yield_percent": None, "missing_reason": "unknown"},
        {"tenor": "90D"},
        {"source_code": "coinbase_exchange"},
        {"dataset_code": "real_yields"},
        {"observed_on": date(1989, 12, 31)},
        {"last_updated_at": NOW - timedelta(seconds=1)},
        {"last_ingestion_run_id": uuid4()},
    ],
)
def test_invalid_yields_are_rejected(connection: Connection, changes: dict[str, Any]) -> None:
    values = treasury_yield_values(connection) | changes
    with pytest.raises((IntegrityError, DataError)), connection.begin_nested():
        connection.execute(treasury_yields.insert().values(**values))


@pytest.mark.parametrize(
    ("value", "reason"),
    [(Decimal(0), None), (None, "source_null"), (None, "field_absent")],
)
def test_zero_and_missing_are_distinct(
    connection: Connection, value: Decimal | None, reason: str | None
) -> None:
    values = treasury_yield_values(connection) | {"yield_percent": value, "missing_reason": reason}
    connection.execute(treasury_yields.insert().values(**values))
    row = connection.execute(sa.select(treasury_yields)).mappings().one()
    assert row["yield_percent"] == value and row["missing_reason"] == reason
    assert row["observed_on"] == date(2024, 1, 2)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(treasury_yields.insert().values(**values))


def test_exact_yield_precision(connection: Connection) -> None:
    values = treasury_yield_values(connection)
    connection.execute(treasury_yields.insert().values(**values))
    assert (
        connection.execute(sa.select(treasury_yields.c.yield_percent)).scalar_one()
        == values["yield_percent"]
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"source_code": "coinbase_exchange"},
        {"dataset_code": "other"},
        {"requested_start": date(2024, 1, 2)},
        {"requested_end": date(2024, 3, 1)},
        {"requested_end": date(2024, 1, 1)},
        {"requested_start": date(1989, 12, 1)},
        {"status": "other"},
        {"status": "succeeded"},
        {"status": "failed"},
        {"finished_at": NOW},
        {"error_code": "http_error"},
        {"status": "failed", "finished_at": NOW},
        {"status": "failed", "finished_at": NOW, "error_code": "secret error text"},
        {"status": "succeeded", "finished_at": NOW - timedelta(seconds=1)},
        {"received_dates": -1},
        {"received_dates": 1},
        {"received_rates": 1},
        {"inserted": 1},
        {"updated": -1},
        {"unchanged": -1},
        {"source_null": 1},
        {"field_absent": -1},
        {"retained_dates": 1},
    ],
)
def test_invalid_month_audit_is_rejected(connection: Connection, changes: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            treasury_ingestion_runs.insert().values(**(treasury_run_values() | changes))
        )


def test_upgrade_preserves_existing_candles(connection: Connection) -> None:
    values = candle_values(connection)
    connection.execute(candles.insert().values(**values))
    config = migration_config()
    config.attributes["connection"] = connection
    # This whole DDL/DML transaction is rolled back by the fixture, never a dev downgrade.
    command.downgrade(config, "0001")
    assert "treasury_yields" not in sa.inspect(connection).get_table_names()
    assert connection.execute(sa.select(candles.c.close)).scalar_one() == values["close"]
    command.upgrade(config, "head")
    assert "treasury_yields" in sa.inspect(connection).get_table_names()
    assert connection.execute(sa.select(candles.c.close)).scalar_one() == values["close"]
    command.check(config)
