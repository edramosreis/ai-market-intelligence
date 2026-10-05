"""Tests use environment-only settings and an explicitly isolated database."""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine

from market_intelligence.cli import initialize_database
from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unit tests must not inherit local .env or credentials from the test runner.
    if os.environ.get("RUN_POSTGRES_TESTS") != "1":
        for key in tuple(os.environ):
            if key.startswith("POSTGRES_"):
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
