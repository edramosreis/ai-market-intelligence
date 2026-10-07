"""Native funding/OI constraints, grants, and compatibility on isolated PostgreSQL."""

from datetime import UTC, datetime, timedelta
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
from market_intelligence.db.tables import (
    candles,
    funding_events,
    funding_ingestion_runs,
    open_interest_runs,
    open_interest_snapshots,
    perpetual_instruments,
    treasury_yields,
)
from tests.integration.test_schema import candle_values
from tests.integration.test_treasury_schema import treasury_yield_values

pytestmark = pytest.mark.integration
START = datetime(2024, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 10, 7, tzinfo=UTC)


def funding_values(conn: Connection) -> dict[str, Any]:
    run_id = uuid4()
    conn.execute(
        funding_ingestion_runs.insert().values(
            id=run_id,
            source_code="hyperliquid",
            instrument_code="BTC-PERP",
            requested_start=START,
            requested_end=START + timedelta(hours=1),
            expected_hours=1,
            started_at=NOW,
            status="running",
        )
    )
    return dict(
        source_code="hyperliquid",
        instrument_code="BTC-PERP",
        event_at=START + timedelta(milliseconds=76),
        settlement_hour=START,
        funding_rate=Decimal("-0.000012500000000001"),
        premium=Decimal(0),
        first_ingested_at=NOW,
        last_updated_at=NOW,
        last_ingestion_run_id=run_id,
    )


def oi_values(conn: Connection) -> dict[str, Any]:
    run_id = uuid4()
    conn.execute(
        open_interest_runs.insert().values(
            id=run_id,
            source_code="hyperliquid",
            instrument_code="BTC-PERP",
            started_at=NOW,
            status="running",
        )
    )
    return dict(
        snapshot_id=uuid4(),
        source_code="hyperliquid",
        instrument_code="BTC-PERP",
        fetch_started_at=NOW,
        received_at=NOW + timedelta(seconds=1),
        open_interest_btc=Decimal(0),
        mark_price_usdt=Decimal("42000.125"),
        oracle_price_usdt=Decimal("42001.25"),
        ingestion_run_id=run_id,
    )


def test_native_contract_times_values_and_uniqueness(connection: Connection) -> None:
    contract = connection.execute(sa.select(perpetual_instruments)).mappings().one()
    assert (
        contract["base_asset"],
        contract["denomination_asset"],
        contract["collateral_asset"],
        contract["settlement_asset"],
    ) == ("BTC", "USDT", "USDC", "USDC")
    values = funding_values(connection)
    connection.execute(funding_events.insert().values(**values))
    row = connection.execute(sa.select(funding_events)).mappings().one()
    assert row["event_at"] == START + timedelta(milliseconds=76)
    assert row["funding_rate"] == values["funding_rate"]
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            funding_events.insert().values(
                **(values | {"event_at": START + timedelta(milliseconds=77)})
            )
        )
    oi = oi_values(connection)
    connection.execute(open_interest_snapshots.insert().values(**oi))
    assert (
        connection.execute(sa.select(open_interest_snapshots.c.open_interest_btc)).scalar_one() == 0
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"event_at": START + timedelta(microseconds=1)},
        {"settlement_hour": START + timedelta(hours=1)},
        {"event_at": START - timedelta(milliseconds=1)},
        {"funding_rate": Decimal("NaN")},
        {"premium": Decimal("Infinity")},
        {"funding_rate": Decimal("1e20")},
        {"instrument_code": "BTC-USD"},
        {"source_code": "coinbase_exchange"},
        {"last_ingestion_run_id": uuid4()},
        {"last_updated_at": NOW - timedelta(seconds=1)},
    ],
)
def test_invalid_funding_fails(connection: Connection, changes: dict[str, Any]) -> None:
    values = funding_values(connection) | changes
    with pytest.raises((IntegrityError, DataError)), connection.begin_nested():
        connection.execute(funding_events.insert().values(**values))


@pytest.mark.parametrize(
    "changes",
    [
        {"received_at": NOW - timedelta(seconds=1)},
        {"open_interest_btc": Decimal(-1)},
        {"open_interest_btc": Decimal("NaN")},
        {"open_interest_btc": Decimal("Infinity")},
        {"mark_price_usdt": Decimal(0)},
        {"oracle_price_usdt": Decimal("Infinity")},
        {"ingestion_run_id": uuid4()},
        {"instrument_code": "ETH-PERP"},
    ],
)
def test_invalid_oi_fails(connection: Connection, changes: dict[str, Any]) -> None:
    values = oi_values(connection) | changes
    with pytest.raises((IntegrityError, DataError)), connection.begin_nested():
        connection.execute(open_interest_snapshots.insert().values(**values))


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "succeeded"},
        {"status": "failed", "finished_at": NOW},
        {"expected_hours": 0},
        {"received": 1},
        {"requested_end": START},
        {"requested_start": START + timedelta(milliseconds=1)},
        {"requested_end": START + timedelta(days=32)},
        {"finished_at": NOW},
    ],
)
def test_invalid_run_lifecycle_and_counts(connection: Connection, changes: dict[str, Any]) -> None:
    values = (
        dict(
            id=uuid4(),
            source_code="hyperliquid",
            instrument_code="BTC-PERP",
            requested_start=START,
            requested_end=START + timedelta(hours=1),
            expected_hours=1,
            started_at=NOW,
            status="running",
        )
        | changes
    )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(funding_ingestion_runs.insert().values(**values))


def test_upgrade_preserves_coinbase_and_treasury(connection: Connection) -> None:
    btc = candle_values(connection)
    connection.execute(candles.insert().values(**btc))
    treasury = treasury_yield_values(connection)
    connection.execute(treasury_yields.insert().values(**treasury))
    config = migration_config()
    config.attributes["connection"] = connection
    command.downgrade(config, "0002")
    assert "funding_events" not in sa.inspect(connection).get_table_names()
    command.upgrade(config, "head")
    assert connection.execute(sa.select(candles.c.close)).scalar_one() == btc["close"]
    assert (
        connection.execute(sa.select(treasury_yields.c.yield_percent)).scalar_one()
        == treasury["yield_percent"]
    )


@pytest.mark.parametrize("role", [DatabaseRole.READ, DatabaseRole.INGEST])
def test_select_only_catalog_and_immutable_oi_grants(
    database_settings: DatabaseSettings, role: DatabaseRole
) -> None:
    engine = create_db_engine(database_settings, role)
    try:
        with engine.connect() as conn, conn.begin():
            for table in (
                perpetual_instruments,
                funding_events,
                funding_ingestion_runs,
                open_interest_runs,
                open_interest_snapshots,
            ):
                conn.execute(sa.select(table).limit(1)).all()
            forbidden = [
                "UPDATE perpetual_instruments SET code = code",
                "DELETE FROM funding_events",
                "UPDATE open_interest_snapshots SET open_interest_btc = open_interest_btc",
            ]
            if role == DatabaseRole.READ:
                forbidden.extend(
                    [
                        "INSERT INTO open_interest_runs DEFAULT VALUES",
                        "UPDATE funding_events SET premium = premium",
                    ]
                )
            else:
                values = funding_values(conn)
                conn.execute(funding_events.insert().values(**values))
                conn.execute(funding_events.update().values(funding_rate=0))
                conn.execute(open_interest_snapshots.insert().values(**oi_values(conn)))
            for statement in forbidden:
                with pytest.raises(ProgrammingError), conn.begin_nested():
                    conn.execute(sa.text(statement))
    finally:
        engine.dispose()
