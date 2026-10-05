from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import DataError, IntegrityError

from market_intelligence.cli import initialize_database, migration_config
from market_intelligence.config import DatabaseSettings
from market_intelligence.db.tables import assets, candles, data_sources, ingestion_runs, markets

pytestmark = pytest.mark.integration

OPENED = datetime(2024, 1, 1, tzinfo=UTC)
INGESTED = datetime(2026, 10, 5, tzinfo=UTC)


def market_id(conn: Connection) -> int:
    return int(conn.execute(sa.select(markets.c.id)).scalar_one())


def run_values(conn: Connection) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "market_id": market_id(conn),
        "interval_seconds": 300,
        "requested_start": OPENED,
        "requested_end": OPENED + timedelta(minutes=5),
        "started_at": INGESTED,
        "status": "running",
    }


def candle_values(conn: Connection) -> dict[str, Any]:
    run = run_values(conn)
    conn.execute(ingestion_runs.insert().values(**run))
    return {
        "market_id": run["market_id"],
        "interval_seconds": 300,
        "opened_at": OPENED,
        "open": Decimal("42123.123456789123456789"),
        "high": Decimal("42200"),
        "low": Decimal("42000"),
        "close": Decimal("42150"),
        "base_volume": Decimal("1.000000000000000001"),
        "first_ingested_at": INGESTED,
        "last_updated_at": INGESTED,
        "last_ingestion_run_id": run["id"],
    }


def test_reference_seed_and_schema_match_migrations(connection: Connection) -> None:
    assert connection.execute(sa.select(data_sources.c.code)).scalars().all() == [
        "coinbase_exchange"
    ]
    assert set(connection.execute(sa.select(assets.c.code)).scalars()) == {"BTC", "USD"}
    market = connection.execute(sa.select(markets)).mappings().one()
    assert market["source_product_id"] == "BTC-USD"
    assert market["base_asset_code"] == "BTC"
    assert market["quote_asset_code"] == "USD"
    config = migration_config()
    config.attributes["connection"] = connection
    command.check(config)


def test_decimal_precision_and_utc_round_trip(connection: Connection) -> None:
    values = candle_values(connection)
    # Same instant expressed at a UTC offset; PostgreSQL/session returns UTC.
    values["opened_at"] = OPENED.astimezone(timezone(timedelta(hours=3)))
    connection.execute(candles.insert().values(**values))
    row = connection.execute(sa.select(candles)).mappings().one()
    assert row["open"] == values["open"]
    assert row["base_volume"] == values["base_volume"]
    assert row["opened_at"] == OPENED
    assert row["opened_at"].utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("interval_seconds", 600),
        ("opened_at", OPENED + timedelta(seconds=1)),
        ("open", Decimal(0)),
        ("low", Decimal(-1)),
        ("open", Decimal("42300")),
        ("close", Decimal("41900")),
        ("base_volume", Decimal(-1)),
        ("last_updated_at", INGESTED - timedelta(seconds=1)),
        ("last_ingestion_run_id", uuid4()),
        ("market_id", -1),
        ("close", None),
        *[(name, Decimal("NaN")) for name in ("open", "high", "low", "close", "base_volume")],
        ("high", Decimal("Infinity")),
    ],
)
def test_invalid_candles_are_rejected(connection: Connection, field: str, value: Any) -> None:
    values = candle_values(connection)
    values[field] = value
    with pytest.raises((DataError, IntegrityError)), connection.begin_nested():
        connection.execute(candles.insert().values(**values))
    assert connection.execute(sa.select(sa.func.count()).select_from(candles)).scalar_one() == 0


def test_candle_identity_is_unique(connection: Connection) -> None:
    values = candle_values(connection)
    connection.execute(candles.insert().values(**values))
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(candles.insert().values(**values))


def test_run_cannot_provide_provenance_for_a_different_market(connection: Connection) -> None:
    values = candle_values(connection)
    second_market = connection.execute(
        markets.insert()
        .values(
            source_code="coinbase_exchange",
            source_product_id="synthetic-test-market",
            base_asset_code="BTC",
            quote_asset_code="USD",
        )
        .returning(markets.c.id)
    ).scalar_one()
    values["market_id"] = second_market
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(candles.insert().values(**values))


@pytest.mark.parametrize(
    "changes",
    [
        {"interval_seconds": 900},
        {"requested_end": OPENED},
        {"requested_start": OPENED + timedelta(seconds=1)},
        {"requested_end": OPENED + timedelta(minutes=5, seconds=1)},
        {"status": "unknown"},
        {"status": "succeeded"},
        {"finished_at": INGESTED},
        {"status": "failed", "finished_at": INGESTED - timedelta(seconds=1)},
        {"received": -1},
        {"inserted": -1},
        {"updated": -1},
        {"unchanged": -1},
        {"missing_buckets": -1},
        {"market_id": -1},
    ],
)
def test_invalid_run_windows_lifecycle_and_counts_are_rejected(
    connection: Connection, changes: dict[str, Any]
) -> None:
    values = run_values(connection) | changes
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(ingestion_runs.insert().values(**values))


def test_candle_and_run_write_roll_back_together(connection: Connection) -> None:
    with pytest.raises(IntegrityError), connection.begin_nested():
        values = candle_values(connection)
        connection.execute(candles.insert().values(**values))
        connection.execute(candles.insert().values(**values))
    for table in (candles, ingestion_runs):
        assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0


def test_source_product_identity_is_unique(connection: Connection) -> None:
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            markets.insert().values(
                source_code="coinbase_exchange",
                source_product_id="BTC-USD",
                base_asset_code="BTC",
                quote_asset_code="USD",
            )
        )


def test_bootstrap_is_repeatable_without_duplicate_reference_data(
    database_settings: DatabaseSettings, admin_engine: Engine
) -> None:
    initialize_database(database_settings)
    with admin_engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(markets)).scalar_one() == 1


def test_migration_can_downgrade_and_reapply_inside_a_rolled_back_transaction(
    connection: Connection,
) -> None:
    config = migration_config()
    config.attributes["connection"] = connection
    command.downgrade(config, "base")
    assert "candles" not in sa.inspect(connection).get_table_names()
    command.upgrade(config, "head")
    assert "candles" in sa.inspect(connection).get_table_names()
    assert connection.execute(sa.select(sa.func.count()).select_from(markets)).scalar_one() == 1
