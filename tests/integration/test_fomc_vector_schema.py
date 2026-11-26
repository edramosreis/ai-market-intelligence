from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection
from sqlalchemy.exc import DBAPIError

from market_intelligence.cli import migration_config
from market_intelligence.db.fomc_processing_store import FomcProcessingStore
from market_intelligence.db.fomc_store import FomcStore
from market_intelligence.db.fomc_vector_schema import STOCK_REVISION, VECTOR_REVISION, VECTOR_TABLES
from market_intelligence.fomc.artifacts import ArtifactStore
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.processing import process_pilot
from tests.db_test_schema import integration_revision
from tests.integration.test_fomc_retrieval import collect_document
from tests.integration.test_fomc_store import store  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def vector_config(connection: Connection) -> Iterator[Config]:
    if integration_revision() != VECTOR_REVISION:
        pytest.skip("Requires the guarded M3 vector test image")
    config = migration_config()
    config.attributes["connection"] = connection
    yield config


def test_stock_postgres_refuses_vector_revision_before_creating_objects(
    connection: Connection,
) -> None:
    if integration_revision() != STOCK_REVISION:
        pytest.skip("Requires stock PostgreSQL")
    config = migration_config()
    config.attributes["connection"] = connection
    with pytest.raises(DBAPIError, match="FOMC vectors require"), connection.begin_nested():
        command.upgrade(config, VECTOR_REVISION)
    assert connection.scalar(sa.text("SELECT to_regnamespace('vector_support')")) is None
    assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == STOCK_REVISION


def test_populated_upgrade_and_empty_vector_downgrade(
    store: FomcStore,  # noqa: F811
    connection: Connection,
    vector_config: Config,
    tmp_path: Path,
) -> None:
    artifacts = ArtifactStore(tmp_path)
    collect_document(store, artifacts, PILOT_DOCUMENTS[0])
    process_pilot(FomcProcessingStore(store.engine), artifacts)
    command.downgrade(vector_config, STOCK_REVISION)
    assert connection.scalar(sa.text("SELECT to_regnamespace('vector_support')")) is None
    rows = connection.exec_driver_sql("SELECT * FROM public.fomc_passages ORDER BY passage_id")
    passages = rows.all()
    assert passages
    previous_path = connection.exec_driver_sql("SHOW search_path").scalar_one()
    command.upgrade(vector_config, VECTOR_REVISION)
    assert connection.exec_driver_sql("SHOW search_path").scalar_one() == previous_path
    assert (
        connection.exec_driver_sql("SELECT * FROM public.fomc_passages ORDER BY passage_id").all()
        == passages
    )
    assert set(VECTOR_TABLES) <= set(sa.inspect(connection).get_table_names())
    for table in VECTOR_TABLES:
        assert connection.exec_driver_sql(f"SELECT count(*) FROM public.{table}").scalar_one() == 0


def test_vector_downgrade_refuses_profile_evidence(
    connection: Connection, vector_config: Config
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO public.fomc_embedding_profiles
                (profile_id, config_sha256, epoch_id, provider, model, dimensions, tokenizer,
                 tokenizer_revision, input_recipe, coordinate_format, distance_metric)
            VALUES (:profile, :config, :epoch, 'synthetic', 'fixture', 3, 'fixture', 'one',
                    'canonical_passage_utf8_v1', 'unit_float32_be_v1', 'cosine')
            """
        ),
        {"profile": "a" * 64, "config": "b" * 64, "epoch": UUID(int=1)},
    )
    with (
        pytest.raises(DBAPIError, match="requires empty embedding tables"),
        connection.begin_nested(),
    ):
        command.downgrade(vector_config, STOCK_REVISION)
    assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == VECTOR_REVISION
