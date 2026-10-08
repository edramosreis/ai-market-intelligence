"""Tests use environment-only settings and an explicitly isolated database."""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine

from market_intelligence.cli import initialize_database
from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.hyperliquid_store import HyperliquidStore
from market_intelligence.db.macro_store import MacroStore
from market_intelligence.db.macro_tables import (
    macro_current,
    macro_ingestion_runs,
    macro_observed_versions,
    macro_version_footnotes,
)
from market_intelligence.db.tables import (
    funding_events,
    funding_ingestion_runs,
    open_interest_runs,
    open_interest_snapshots,
    treasury_ingestion_runs,
    treasury_yields,
)
from market_intelligence.db.treasury_store import TreasuryStore


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unit tests must not inherit local .env or credentials from the test runner.
    for key in tuple(os.environ):
        if key.startswith(("OPENAI_", "AGENT_")):
            monkeypatch.delenv(key)
    if os.environ.get("RUN_POSTGRES_TESTS") != "1":
        for key in tuple(os.environ):
            if key.startswith(("POSTGRES_", "API_")):
                monkeypatch.delenv(key)


@pytest.fixture(scope="session")
def database_settings() -> DatabaseSettings:
    if os.environ.get("RUN_POSTGRES_TESTS") != "1":
        pytest.skip("Run PostgreSQL integration tests with compose.test.yaml")
    settings = DatabaseSettings(_env_file=None)  # type: ignore[call-arg]
    if settings.db != "market_intelligence_test" or settings.host != "db-test":
        pytest.fail("Refusing integration tests outside the isolated Compose test database")
    initialize_database(settings)
    return settings


@pytest.fixture(scope="session")
def admin_engine(database_settings: DatabaseSettings) -> Iterator[Engine]:
    engine = create_db_engine(database_settings, DatabaseRole.ADMIN)
    yield engine
    engine.dispose()


@pytest.fixture
def connection(admin_engine: Engine) -> Iterator[Connection]:
    with admin_engine.connect() as conn, conn.begin() as transaction:
        yield conn
        transaction.rollback()


@pytest.fixture
def treasury_store(
    database_settings: DatabaseSettings, admin_engine: Engine
) -> Iterator[TreasuryStore]:
    engine = create_db_engine(database_settings, DatabaseRole.INGEST)
    try:
        yield TreasuryStore(engine)
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            conn.execute(treasury_yields.delete())
            conn.execute(treasury_ingestion_runs.delete())


@pytest.fixture
def hyperliquid_store(
    database_settings: DatabaseSettings, admin_engine: Engine
) -> Iterator[HyperliquidStore]:
    engine = create_db_engine(database_settings, DatabaseRole.INGEST)
    try:
        yield HyperliquidStore(engine)
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            for table in (
                funding_events,
                funding_ingestion_runs,
                open_interest_snapshots,
                open_interest_runs,
            ):
                conn.execute(table.delete())


@pytest.fixture
def macro_store(database_settings: DatabaseSettings, admin_engine: Engine) -> Iterator[MacroStore]:
    engine = create_db_engine(database_settings, DatabaseRole.INGEST)
    try:
        yield MacroStore(engine)
    finally:
        engine.dispose()
        with admin_engine.begin() as conn:
            for table in (
                macro_current,
                macro_version_footnotes,
                macro_observed_versions,
                macro_ingestion_runs,
            ):
                conn.execute(table.delete())
