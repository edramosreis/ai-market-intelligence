from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import Connection, Engine

from market_intelligence.cli import migration_config
from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.fomc_processing_tables import PROCESSING_TABLE_NAMES
from market_intelligence.db.fomc_store import FomcStore
from market_intelligence.db.tables import candles, metadata
from market_intelligence.fomc.artifacts import ArtifactStore
from tests.integration.test_fomc_store import NOW, load, store  # noqa: F401
from tests.integration.test_schema import candle_values

pytestmark = pytest.mark.integration


def test_populated_0006_upgrade_preserves_existing_data(
    store: FomcStore,  # noqa: F811
    connection: Connection,
    tmp_path: Path,
) -> None:
    load(store, ArtifactStore(tmp_path), NOW)
    connection.execute(candles.insert().values(**candle_values(connection)))
    config = migration_config()
    config.attributes["connection"] = connection
    command.downgrade(config, "0006")
    before = {
        table.name: connection.execute(sa.select(table)).mappings().all()
        for table in metadata.tables.values()
        if table.name not in PROCESSING_TABLE_NAMES
    }
    assert not set(PROCESSING_TABLE_NAMES) & set(sa.inspect(connection).get_table_names())
    command.upgrade(config, "0007")
    for name, rows in before.items():
        assert connection.execute(sa.select(metadata.tables[name])).mappings().all() == rows
    for name in PROCESSING_TABLE_NAMES:
        assert connection.execute(sa.select(metadata.tables[name])).first() is None
    command.upgrade(config, "head")
    command.check(config)


def test_processing_tables_are_append_only_for_writer(
    database_settings: DatabaseSettings, admin_engine: Engine
) -> None:
    for role, expected in (
        (DatabaseRole.READ, {"SELECT"}),
        (DatabaseRole.INGEST, {"SELECT", "INSERT"}),
    ):
        account = database_settings.credentials(role)[0]
        with admin_engine.connect() as conn:
            for table in PROCESSING_TABLE_NAMES:
                allowed = {
                    privilege
                    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                    if conn.scalar(
                        sa.text("SELECT has_table_privilege(:account, :table, :privilege)"),
                        {"account": account, "table": table, "privilege": privilege},
                    )
                }
                assert allowed == expected
