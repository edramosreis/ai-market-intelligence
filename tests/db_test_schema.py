"""Run the same integration checks on either reviewed database image."""

import os

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext

from market_intelligence.db.fomc_vector_schema import STOCK_REVISION, VECTOR_REVISION
from market_intelligence.db.tables import metadata


def integration_revision() -> str:
    return VECTOR_REVISION if os.environ.get("RUN_VECTOR_TESTS") == "1" else STOCK_REVISION


def check_schema(config: Config) -> None:
    if integration_revision() == VECTOR_REVISION:
        command.check(config)
        return
    # Alembic's check command requires the latest revision even when a test
    # deliberately uses the stock PostgreSQL image and its compatible schema.
    migration = MigrationContext.configure(config.attributes["connection"])
    assert migration.get_current_revision() == STOCK_REVISION
    assert compare_metadata(migration, metadata) == []
