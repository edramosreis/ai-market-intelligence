import argparse
from collections.abc import Sequence
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.setup import grant_table_access, provision_roles


def migration_config() -> Config:
    path = Path("alembic.ini")
    if not path.is_file():
        raise ValueError("Run database commands from the repository root")
    return Config(str(path))


def initialize_database(settings: DatabaseSettings) -> None:
    config = migration_config()
    provision_roles(settings)
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    grant_table_access(settings)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Market intelligence database foundation")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init-db", help="Provision local roles, migrate, and install grants")
    check = subcommands.add_parser("check-db", help="Verify connection and schema revision")
    check.add_argument("--role", choices=[role.value for role in DatabaseRole], default="read")
    args = parser.parse_args(argv)
    try:
        settings = DatabaseSettings()  # type: ignore[call-arg]
        if args.command == "init-db":
            initialize_database(settings)
            print("Database initialized; migrations and role grants applied.")
        else:
            engine = create_db_engine(settings, DatabaseRole(args.role))
            try:
                with engine.connect() as conn:
                    revision = conn.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                    market_count = conn.execute(text("SELECT count(*) FROM markets")).scalar_one()
                print(
                    f"Database ready: revision={revision}, markets={market_count}, role={args.role}"
                )
            finally:
                engine.dispose()
    except ValueError, RuntimeError, SQLAlchemyError, CommandError:
        parser.exit(
            1, "Database command failed. Check configuration, service health, and migrations.\n"
        )
    return 0
