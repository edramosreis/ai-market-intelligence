from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.schema import SchemaItem

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.fomc_processing_tables import fomc_passages  # noqa: F401
from market_intelligence.db.fomc_tables import fomc_documents  # noqa: F401
from market_intelligence.db.fomc_vector_schema import SOURCE_KEY, VECTOR_TABLES
from market_intelligence.db.macro_tables import macro_series  # noqa: F401
from market_intelligence.db.scheduler_tables import scheduler_job_state  # noqa: F401
from market_intelligence.db.tables import metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    # The extension tables are managed by reviewed SQL and checked with actual
    # PostgreSQL constraints, because stock SQLAlchemy cannot reflect pgvector.
    return not (type_ == "table" and name in VECTOR_TABLES)


def include_object(
    obj: SchemaItem,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: SchemaItem | None,
) -> bool:
    return not (type_ == "unique_constraint" and name == SOURCE_KEY)


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        compare_type=True,
        include_name=include_name,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        dialect_name="postgresql",
        target_metadata=metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()
elif "connection" in config.attributes:
    run_migrations(config.attributes["connection"])
else:
    settings = config.attributes.get("settings") or DatabaseSettings()  # type: ignore[call-arg]
    engine = create_db_engine(settings, DatabaseRole.ADMIN)
    try:
        with engine.connect() as connection:
            run_migrations(connection)
    finally:
        engine.dispose()
