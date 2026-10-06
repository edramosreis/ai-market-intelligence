from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.exc import ProgrammingError

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.tables import (
    candles,
    ingestion_runs,
    markets,
    treasury_ingestion_runs,
    treasury_yields,
)
from tests.integration.test_schema import candle_values
from tests.integration.test_treasury_schema import treasury_yield_values

pytestmark = pytest.mark.integration


@pytest.fixture
def reader(database_settings: DatabaseSettings) -> Iterator[Connection]:
    engine = create_db_engine(database_settings, DatabaseRole.READ)
    try:
        with engine.connect() as conn, conn.begin() as transaction:
            yield conn
            transaction.rollback()
    finally:
        engine.dispose()


@pytest.fixture
def writer(database_settings: DatabaseSettings) -> Iterator[Connection]:
    engine = create_db_engine(database_settings, DatabaseRole.INGEST)
    try:
        with engine.connect() as conn, conn.begin() as transaction:
            yield conn
            transaction.rollback()
    finally:
        engine.dispose()


def test_reader_can_read_seed_but_has_no_elevated_privileges(reader: Connection) -> None:
    assert reader.execute(sa.select(markets.c.source_product_id)).scalar_one() == "BTC-USD"
    privileges = reader.execute(
        sa.text(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
    ).one()
    assert not any(privileges)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO data_sources VALUES ('test', 'test')",
        "UPDATE candles SET close = close",
        "DELETE FROM candles",
        "INSERT INTO ingestion_runs DEFAULT VALUES",
        "INSERT INTO treasury_ingestion_runs DEFAULT VALUES",
        "UPDATE treasury_yields SET yield_percent = yield_percent",
        "DELETE FROM treasury_yields",
        "CREATE TABLE unauthorized (id integer)",
        "CREATE TEMP TABLE unauthorized (id integer)",
    ],
)
def test_reader_cannot_write_or_create_tables(reader: Connection, statement: str) -> None:
    with pytest.raises(ProgrammingError), reader.begin_nested():
        reader.execute(sa.text(statement))


def test_writer_can_insert_and_update_candles_and_runs(writer: Connection) -> None:
    values = candle_values(writer)
    writer.execute(candles.insert().values(**values))
    writer.execute(candles.update().values(base_volume=2))
    writer.execute(
        ingestion_runs.update().values(status="succeeded", finished_at=values["last_updated_at"])
    )
    assert writer.execute(sa.select(candles.c.base_volume)).scalar_one() == 2


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO data_sources VALUES ('test', 'test')",
        "UPDATE markets SET source_product_id = source_product_id",
        "DELETE FROM candles",
        "DELETE FROM ingestion_runs",
        "DELETE FROM treasury_yields",
        "DELETE FROM treasury_ingestion_runs",
        "CREATE TABLE unauthorized (id integer)",
        "CREATE TEMP TABLE unauthorized (id integer)",
    ],
)
def test_writer_cannot_mutate_references_delete_or_create_tables(
    writer: Connection, statement: str
) -> None:
    with pytest.raises(ProgrammingError), writer.begin_nested():
        writer.execute(sa.text(statement))


def test_treasury_writer_and_reader_grants(writer: Connection, reader: Connection) -> None:
    values = treasury_yield_values(writer)
    writer.execute(treasury_yields.insert().values(**values))
    writer.execute(treasury_yields.update().values(yield_percent=0))
    writer.execute(
        treasury_ingestion_runs.update().values(
            status="failed", finished_at=values["last_updated_at"], error_code="http_error"
        )
    )
    assert writer.execute(sa.select(treasury_yields.c.yield_percent)).scalar_one() == 0
    # The other role can read both tables, but cannot see this uncommitted fixture.
    for table in (treasury_yields, treasury_ingestion_runs):
        reader.execute(sa.select(table).limit(1)).all()
