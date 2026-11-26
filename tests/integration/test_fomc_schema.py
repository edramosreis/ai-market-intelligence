from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from market_intelligence.cli import migration_config
from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.fomc_tables import TABLES, fomc_documents, fomc_ingestion_runs
from market_intelligence.db.tables import candles, metadata
from market_intelligence.fomc.catalog import ADDITIONAL_BATCHES, CATALOG_DOCUMENTS, PILOT_DOCUMENTS
from tests.db_test_schema import check_schema, integration_revision
from tests.integration.test_schema import candle_values

pytestmark = pytest.mark.integration


def run_values() -> dict[str, Any]:
    return dict(
        run_id=uuid4(),
        started_at=datetime(2026, 10, 20, tzinfo=UTC),
        status="running",
        requested_ids=[d.document_id for d in PILOT_DOCUMENTS],
    )


def test_catalog_and_populated_0005_upgrade_preserve_native_tables(connection: Connection) -> None:
    connection.execute(candles.insert().values(**candle_values(connection)))
    native = [table for name, table in metadata.tables.items() if not name.startswith("fomc_")]
    before = {table.name: connection.execute(sa.select(table)).mappings().all() for table in native}
    config = migration_config()
    config.attributes["connection"] = connection
    command.downgrade(config, "0005")
    assert "fomc_documents" not in sa.inspect(connection).get_table_names()
    command.upgrade(config, integration_revision())
    assert {
        table.name: connection.execute(sa.select(table)).mappings().all() for table in native
    } == before
    rows = connection.execute(sa.select(fomc_documents)).mappings().all()
    assert {row["document_id"]: dict(row) for row in rows} == {
        spec.document_id: dict(
            document_id=spec.document_id,
            kind=spec.kind,
            meeting_start=spec.meeting_start,
            meeting_end=spec.meeting_end,
            published_on=spec.published_on,
            publication_precision=spec.publication_precision,
            official_url=spec.url,
            publication_evidence_url=spec.publication_evidence_url,
            explicit_release_time_text=spec.release_time_text,
        )
        for spec in CATALOG_DOCUMENTS
    }
    check_schema(config)


def test_catalog_upgrade_preserves_old_rows_and_refuses_loss_of_batch_audit(
    connection: Connection,
) -> None:
    config = migration_config()
    config.attributes["connection"] = connection
    command.downgrade(config, "0007")
    before = connection.execute(sa.select(fomc_documents)).mappings().all()
    original_run = run_values() | {
        "status": "failed",
        "finished_at": datetime(2026, 10, 20, tzinfo=UTC),
        "error_code": "interrupted",
    }
    connection.execute(fomc_ingestion_runs.insert().values(original_run))
    command.upgrade(config, integration_revision())
    rows = connection.execute(sa.select(fomc_documents)).mappings().all()
    assert len(before) == 4 and len(rows) == 16
    assert all(row in rows for row in before)
    assert dict(
        connection.execute(sa.select(fomc_ingestion_runs)).mappings().one()
    ) == original_run | {
        "documents_completed": 0,
        "http_attempts": 0,
        "wire_bytes": 0,
        "entity_bytes": 0,
    }
    added = ADDITIONAL_BATCHES["2024-apr-may-dec"]
    connection.execute(
        fomc_ingestion_runs.insert().values(
            original_run | {"run_id": uuid4(), "requested_ids": [doc.document_id for doc in added]}
        )
    )
    with pytest.raises(DBAPIError, match="referenced by audits"), connection.begin_nested():
        command.downgrade(config, "0007")
    assert connection.scalar(sa.select(sa.func.count()).select_from(fomc_documents)) == 16


def test_only_one_durable_running_command(connection: Connection) -> None:
    connection.execute(fomc_ingestion_runs.insert().values(**run_values()))
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(fomc_ingestion_runs.insert().values(**run_values()))


@pytest.mark.parametrize(
    "changes",
    [
        {"http_attempts": 9},
        {"documents_completed": 5},
        {"wire_bytes": -1},
        {"status": "succeeded", "finished_at": datetime(2026, 10, 20, tzinfo=UTC)},
        {"status": "failed", "finished_at": datetime(2026, 10, 20, tzinfo=UTC)},
        {"requested_ids": ["one"]},
        {"status": "failed", "error_code": "secret error text"},
    ],
)
def test_run_constraints(connection: Connection, changes: dict[str, Any]) -> None:
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(fomc_ingestion_runs.insert().values(**(run_values() | changes)))


def test_reader_and_writer_grants(
    database_settings: DatabaseSettings, admin_engine: Engine
) -> None:
    writer = database_settings.credentials(DatabaseRole.INGEST)[0]
    reader = database_settings.credentials(DatabaseRole.READ)[0]
    with admin_engine.connect() as conn:
        for table in TABLES:
            for role in [reader, writer]:
                assert conn.execute(
                    sa.text("SELECT has_table_privilege(:role,:table,:priv)"),
                    dict(role=role, table=table, priv="SELECT"),
                ).scalar_one()
            for privilege in ["INSERT", "UPDATE", "DELETE"]:
                assert not conn.execute(
                    sa.text("SELECT has_table_privilege(:role,:table,:priv)"),
                    dict(role=reader, table=table, priv=privilege),
                ).scalar_one()
            assert not conn.execute(
                sa.text("SELECT has_table_privilege(:role,:table,:priv)"),
                dict(role=writer, table=table, priv="DELETE"),
            ).scalar_one()
        for table in ["fomc_raw_versions", "fomc_fetch_receipts", "fomc_documents"]:
            assert not conn.execute(
                sa.text("SELECT has_table_privilege(:role,:table,:priv)"),
                dict(role=writer, table=table, priv="UPDATE"),
            ).scalar_one()
    engine = create_db_engine(database_settings, DatabaseRole.INGEST)
    try:
        with pytest.raises(DBAPIError), engine.begin() as conn:
            conn.execute(fomc_documents.update().values(kind="minutes"))
    finally:
        engine.dispose()
