"""Build and database checks for the opt-in, disposable vector test image."""

import math
import os
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.exc import DBAPIError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("RUN_VECTOR_TESTS") != "1",
        reason="Requires the guarded M3 vector test image",
    ),
]


@pytest.fixture
def vector_catalog(connection: Connection) -> Connection:
    server = connection.exec_driver_sql(
        """
        SELECT current_database(), current_user, current_setting('server_version_num')
        """
    ).one()
    assert tuple(server) == ("market_intelligence_test", "m3_test_admin", "180006")
    assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == "0008"
    assert (
        connection.exec_driver_sql(
            "SELECT default_version FROM pg_available_extensions WHERE name = 'vector'"
        ).scalar_one()
        == "0.8.7"
    )
    assert (
        connection.exec_driver_sql(
            "SELECT count(*) FROM pg_extension WHERE extname = 'vector'"
        ).scalar_one()
        == 0
    )
    connection.exec_driver_sql("CREATE SCHEMA vector_support")
    connection.exec_driver_sql("REVOKE ALL ON SCHEMA vector_support FROM PUBLIC")
    connection.exec_driver_sql("CREATE EXTENSION vector WITH SCHEMA vector_support VERSION '0.8.7'")
    connection.exec_driver_sql(
        """
        CREATE TABLE vector_support.sample_passages (
            passage_id text PRIMARY KEY,
            published_on date NOT NULL,
            available boolean NOT NULL,
            generation uuid NOT NULL,
            embedding vector_support.vector(3) NOT NULL
                CHECK (vector_support.vector_norm(embedding) > 0)
        )
        """
    )
    return connection


def test_pgvector_schema_and_existing_catalog_are_available(vector_catalog: Connection) -> None:
    extension = vector_catalog.exec_driver_sql(
        """
        SELECT extversion, nspname
        FROM pg_extension JOIN pg_namespace ON extnamespace = pg_namespace.oid
        WHERE extname = 'vector'
        """
    ).one()
    assert tuple(extension) == ("0.8.7", "vector_support")
    assert vector_catalog.exec_driver_sql("SELECT count(*) FROM fomc_documents").scalar_one() == 16


def test_exact_cosine_with_filters_and_reproducible_ties(vector_catalog: Connection) -> None:
    generation = UUID(int=1)
    vector_catalog.execute(
        sa.text(
            """
            INSERT INTO vector_support.sample_passages
            VALUES (:passage_id, DATE '2024-10-09', :available, :generation,
                    CAST(:embedding AS vector_support.vector(3)))
            """
        ),
        [
            dict(passage_id="a", available=True, generation=generation, embedding="[1,0,0]"),
            dict(passage_id="b", available=True, generation=generation, embedding="[2,0,0]"),
            dict(passage_id="c", available=True, generation=generation, embedding="[1,1,0]"),
            dict(passage_id="d", available=False, generation=generation, embedding="[1,0,0]"),
            dict(passage_id="e", available=True, generation=UUID(int=2), embedding="[1,0,0]"),
        ],
    )
    vector_catalog.execute(
        sa.text(
            """
            INSERT INTO vector_support.sample_passages
            VALUES ('f', DATE '2024-10-10', TRUE, :generation, '[1,0,0]')
            """
        ),
        {"generation": generation},
    )
    found = vector_catalog.execute(
        sa.text(
            """
            SELECT passage_id, embedding OPERATOR(vector_support.<=>)
                CAST(:query AS vector_support.vector(3)) AS distance
            FROM vector_support.sample_passages
            WHERE available AND generation = :generation AND published_on = DATE '2024-10-09'
            ORDER BY distance ASC, passage_id COLLATE "C" ASC
            LIMIT 3
            """
        ),
        {"query": "[1,0,0]", "generation": generation},
    ).all()
    assert [row.passage_id for row in found] == ["a", "b", "c"]
    assert [row.distance for row in found] == pytest.approx([0, 0, 1 - 1 / math.sqrt(2)])


@pytest.mark.parametrize("coordinates", ["[1,0]", "[NaN,1,0]", "[Infinity,1,0]", "[0,0,0]"])
def test_invalid_coordinates_are_rejected(vector_catalog: Connection, coordinates: str) -> None:
    statement = sa.text(
        """
        INSERT INTO vector_support.sample_passages
        VALUES ('x', DATE '2024-10-09', TRUE, :generation,
                CAST(:embedding AS vector_support.vector(3)))
        """
    )
    with pytest.raises(DBAPIError), vector_catalog.begin_nested():
        vector_catalog.execute(statement, {"embedding": coordinates, "generation": UUID(int=1)})


def test_reader_can_use_vector_type_without_write_or_ddl(vector_catalog: Connection) -> None:
    vector_catalog.exec_driver_sql("GRANT USAGE ON SCHEMA vector_support TO m3_test_reader")
    vector_catalog.exec_driver_sql(
        "GRANT SELECT ON vector_support.sample_passages TO m3_test_reader"
    )
    vector_catalog.exec_driver_sql("SET LOCAL ROLE m3_test_reader")
    assert (
        vector_catalog.exec_driver_sql(
            "SELECT vector_support.vector_dims('[1,0,0]'::vector_support.vector)"
        ).scalar_one()
        == 3
    )
    assert (
        vector_catalog.exec_driver_sql(
            "SELECT count(*) FROM vector_support.sample_passages"
        ).scalar_one()
        == 0
    )
    with pytest.raises(DBAPIError, match="permission denied"), vector_catalog.begin_nested():
        vector_catalog.exec_driver_sql("DELETE FROM vector_support.sample_passages")
    with pytest.raises(DBAPIError, match="permission denied"), vector_catalog.begin_nested():
        vector_catalog.exec_driver_sql("CREATE TABLE vector_support.refused (n int)")
