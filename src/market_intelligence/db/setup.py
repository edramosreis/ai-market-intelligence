"""Local role provisioning and least-privilege grants, separate from schema DDL."""

import psycopg
from psycopg import sql

from market_intelligence.config import DatabaseRole, DatabaseSettings

TABLE_NAMES = (
    "data_sources",
    "assets",
    "markets",
    "ingestion_runs",
    "candles",
    "treasury_ingestion_runs",
    "treasury_yields",
    "perpetual_instruments",
    "funding_ingestion_runs",
    "funding_events",
    "open_interest_runs",
    "open_interest_snapshots",
    "macro_series",
    "macro_ingestion_runs",
    "macro_observed_versions",
    "macro_version_footnotes",
    "macro_current",
    "scheduler_job_state",
    "scheduled_job_runs",
)


def provision_roles(settings: DatabaseSettings) -> None:
    admin_name, ingest_name, read_name = settings.validate_role_names()
    admin_url = settings.url(DatabaseRole.ADMIN)
    try:
        with psycopg.connect(
            host=settings.host,
            port=settings.port,
            dbname=settings.db,
            user=admin_name,
            password=admin_url.password,
            connect_timeout=5,
        ) as conn:
            for role in (DatabaseRole.INGEST, DatabaseRole.READ):
                name, password = settings.credentials(role)
                exists = conn.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)
                ).fetchone()
                if exists is None:
                    conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(name)))
                conn.execute(
                    sql.SQL(
                        "ALTER ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                    ).format(sql.Identifier(name), sql.Literal(password.get_secret_value()))
                )
            conn.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(settings.db))
            )
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                    sql.Identifier(settings.db),
                    sql.Identifier(ingest_name),
                    sql.Identifier(read_name),
                )
            )
            conn.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}, {}").format(
                    sql.Identifier(ingest_name), sql.Identifier(read_name)
                )
            )
    except psycopg.Error:
        # Role DDL contains passwords. Do not surface SQL or connection details on failure.
        raise RuntimeError("Database role provisioning failed; check local configuration") from None


def grant_table_access(settings: DatabaseSettings) -> None:
    admin_name, ingest_name, read_name = settings.validate_role_names()
    try:
        with psycopg.connect(
            host=settings.host,
            port=settings.port,
            dbname=settings.db,
            user=admin_name,
            password=settings.credentials(DatabaseRole.ADMIN)[1].get_secret_value(),
            connect_timeout=5,
        ) as conn:
            conn.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC")
            for name in (ingest_name, read_name):
                conn.execute(
                    sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(
                        sql.Identifier(name)
                    )
                )
                conn.execute(
                    sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}").format(
                        sql.Identifier(name)
                    )
                )
                for table in (*TABLE_NAMES, "alembic_version"):
                    conn.execute(
                        sql.SQL("GRANT SELECT ON TABLE public.{} TO {}").format(
                            sql.Identifier(table), sql.Identifier(name)
                        )
                    )
            for table in (
                "candles",
                "ingestion_runs",
                "treasury_yields",
                "treasury_ingestion_runs",
                "funding_events",
                "funding_ingestion_runs",
                "open_interest_runs",
                "macro_ingestion_runs",
                "macro_current",
                "scheduled_job_runs",
            ):
                conn.execute(
                    sql.SQL("GRANT INSERT, UPDATE ON TABLE public.{} TO {}").format(
                        sql.Identifier(table), sql.Identifier(ingest_name)
                    )
                )
            conn.execute(
                sql.SQL("GRANT INSERT ON TABLE public.open_interest_snapshots TO {}").format(
                    sql.Identifier(ingest_name)
                )
            )
            for table in ("macro_observed_versions", "macro_version_footnotes"):
                conn.execute(
                    sql.SQL("GRANT INSERT ON TABLE public.{} TO {}").format(
                        sql.Identifier(table), sql.Identifier(ingest_name)
                    )
                )
            conn.execute(
                sql.SQL("GRANT UPDATE ON TABLE public.scheduler_job_state TO {}").format(
                    sql.Identifier(ingest_name)
                )
            )
    except psycopg.Error:
        raise RuntimeError("Database permission setup failed") from None
