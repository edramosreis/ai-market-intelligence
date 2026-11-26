"""Explicit migration and read-only checks for the isolated M3 vector database."""

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.fomc_vector_schema import STOCK_REVISION, VECTOR_REVISION, VECTOR_TABLES
from market_intelligence.db.setup import grant_table_access

_DATABASE = "market_intelligence_m3"
_ROLES = ("m3_admin", "m3_ingest", "m3_reader")
_PG_VERSION = "180006"
_VECTOR_VERSION = "0.8.7"
_MIGRATION_LOCK = 937240009


def _require_runtime_target(settings: DatabaseSettings) -> None:
    if (settings.host, settings.port, settings.db) != ("db", 5432, _DATABASE):
        raise ValueError("Vector runtime requires the isolated M3 database")


def _require_identity(conn: Connection, username: str) -> None:
    identity = conn.execute(sa.text("SELECT current_database(), current_user")).one()
    if tuple(identity) != (_DATABASE, username):
        raise ValueError("Vector runtime connected to an unexpected database or role")


def vector_runtime_revision(conn: Connection) -> str:
    """Verify the extension prerequisite and distinguish ready from upgradable."""
    server = conn.execute(
        sa.text(
            """
            SELECT current_setting('server_version_num'),
                   EXISTS (SELECT 1 FROM pg_available_extension_versions
                           WHERE name = 'vector' AND version = :version),
                   to_regnamespace('vector_support') IS NOT NULL
            """
        ),
        {"version": _VECTOR_VERSION},
    ).one()
    if server[0] != _PG_VERSION or not server[1]:
        raise ValueError("Vector runtime requires PostgreSQL 18.6 with pgvector 0.8.7 available")
    installed = conn.execute(
        sa.text(
            """
            SELECT e.extversion, n.nspname FROM pg_extension e
            JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'
            """
        )
    ).one_or_none()
    revisions = (
        conn.execute(sa.text("SELECT version_num FROM public.alembic_version")).scalars().all()
    )
    if revisions == [STOCK_REVISION] and installed is None and not server[2]:
        return STOCK_REVISION
    if revisions == [VECTOR_REVISION] and installed == (_VECTOR_VERSION, "vector_support"):
        return VECTOR_REVISION
    raise ValueError("Vector runtime has an unexpected database revision or extension")


def verify_vector_runtime(conn: Connection) -> None:
    if vector_runtime_revision(conn) != VECTOR_REVISION:
        raise ValueError("Vector runtime migration is pending")
    for table in VECTOR_TABLES:
        conn.exec_driver_sql(f"SELECT 1 FROM public.{table} LIMIT 1").all()
    distance = conn.exec_driver_sql(
        """
        SELECT '[1,0,0]'::vector_support.vector OPERATOR(vector_support.<=>)
               '[0,1,0]'::vector_support.vector
        """
    ).scalar_one()
    if distance != 1.0:
        raise ValueError("Vector runtime failed its read-only cosine check")


def _apply_vector_schema(conn: Connection, config: Config) -> None:
    conn.execute(sa.text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": _MIGRATION_LOCK})
    if vector_runtime_revision(conn) == STOCK_REVISION:
        connected = Config(
            config.config_file_name, attributes={**config.attributes, "connection": conn}
        )
        command.upgrade(connected, VECTOR_REVISION)
    verify_vector_runtime(conn)


def initialize_vector_runtime(settings: DatabaseSettings, config: Config) -> None:
    _require_runtime_target(settings)
    if settings.validate_role_names() != _ROLES:
        raise ValueError("Vector runtime requires the existing M3 roles")
    engine = create_db_engine(settings, DatabaseRole.ADMIN)
    try:
        with engine.begin() as conn:
            _require_identity(conn, _ROLES[0])
            _apply_vector_schema(conn, config)
    finally:
        engine.dispose()
    grant_table_access(settings)


def check_vector_runtime(settings: DatabaseSettings) -> None:
    _require_runtime_target(settings)
    if settings.credentials(DatabaseRole.READ)[0] != _ROLES[2]:
        raise ValueError("Vector runtime check requires the M3 reader role")
    engine = create_db_engine(settings, DatabaseRole.READ)
    try:
        with (
            engine.connect().execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            ) as conn,
            conn.begin(),
        ):
            _require_identity(conn, _ROLES[2])
            if conn.exec_driver_sql("SHOW transaction_read_only").scalar_one() != "on":
                raise ValueError("Vector runtime check requires a read-only snapshot")
            verify_vector_runtime(conn)
    finally:
        engine.dispose()
