import pytest
from alembic import command
from sqlalchemy import Connection

from market_intelligence.cli import migration_config
from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.fomc_vector_runtime import (
    _apply_vector_schema,
    vector_runtime_revision,
    verify_vector_runtime,
)
from market_intelligence.db.fomc_vector_schema import STOCK_REVISION, VECTOR_REVISION
from tests.db_test_schema import integration_revision

pytestmark = pytest.mark.integration


def test_vector_runtime_refuses_stock_and_empty_upgrade_requires_migration(
    connection: Connection,
) -> None:
    if integration_revision() == STOCK_REVISION:
        with pytest.raises(ValueError, match="requires PostgreSQL 18.6 with pgvector"):
            vector_runtime_revision(connection)
        return
    assert vector_runtime_revision(connection) == VECTOR_REVISION
    config = migration_config()
    config.attributes["connection"] = connection
    command.downgrade(config, STOCK_REVISION)
    assert vector_runtime_revision(connection) == STOCK_REVISION
    with pytest.raises(ValueError, match="migration is pending"):
        verify_vector_runtime(connection)
    _apply_vector_schema(connection, config)
    _apply_vector_schema(connection, config)
    verify_vector_runtime(connection)


def test_vector_runtime_cosine_check_uses_actual_read_only_role(
    database_settings: DatabaseSettings,
) -> None:
    if integration_revision() != VECTOR_REVISION:
        pytest.skip("Requires the guarded pgvector database")
    engine = create_db_engine(database_settings, DatabaseRole.READ)
    try:
        with (
            engine.connect().execution_options(postgresql_readonly=True) as conn,
            conn.begin(),
        ):
            assert conn.exec_driver_sql("SHOW transaction_read_only").scalar_one() == "on"
            verify_vector_runtime(conn)
    finally:
        engine.dispose()
