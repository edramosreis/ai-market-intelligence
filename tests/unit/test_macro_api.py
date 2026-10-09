"""Macro HTTP validation and database failures expose only controlled errors."""

from typing import cast
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from market_intelligence.api import create_app, get_macro_queries
from market_intelligence.config import AgentSettings, ApiSettings
from market_intelligence.macro.queries import MacroQueries


@pytest.fixture
def client() -> TestClient:
    return TestClient(
        create_app(
            engine=cast(Engine, object()),
            api_settings=ApiSettings(_env_file=None),  # type: ignore[call-arg]
            agent_settings=AgentSettings(_env_file=None),  # type: ignore[call-arg]
        )
    )


@pytest.mark.parametrize(
    "path,method",
    [
        ("/v1/macro/series", "list_series"),
        ("/v1/macro/series/CUSR0000SA0/latest", "latest"),
        (
            "/v1/macro/series/CUSR0000SA0/observations?start=2024-01-01&end=2025-01-01",
            "observations",
        ),
        ("/v1/macro/series/CUSR0000SA0/versions?month=2024-01-01", "observed_versions"),
    ],
)
def test_macro_database_errors_are_sanitized(client: TestClient, path: str, method: str) -> None:
    queries = Mock(spec=MacroQueries)
    getattr(queries, method).side_effect = OperationalError(
        "secret SQL", {"password": "sensitive"}, Exception("private")
    )
    cast(FastAPI, client.app).dependency_overrides[get_macro_queries] = lambda: queries
    with client:
        response = client.get(path)
    assert response.status_code == 503 and response.json() == {"detail": "Database unavailable"}


@pytest.mark.parametrize(
    "suffix",
    ["latest", "observations?start=2024-01-01&end=2025-01-01", "versions?month=2024-01-01"],
)
def test_unknown_series_is_404_without_sql_access(client: TestClient, suffix: str) -> None:
    with client:
        response = client.get(f"/v1/macro/series/unknown/{suffix}")
    assert response.status_code == 404 and response.json() == {"detail": "Unknown macro series"}


@pytest.mark.parametrize(
    "suffix",
    [
        "observations?start=2024-01-02&end=2025-01-01",
        "observations?start=2024-01-01&end=2024-01-01",
        "observations?start=2024-01-01&end=2025-01-01&limit=0",
        "observations?start=2024-01-01&end=2025-01-01&limit=101",
        "observations?start=2024-01-01&end=2025-01-01&cursor=bad!",
        "versions?month=2024-01-02",
        "versions?month=2024-01-01&cursor=bad!",
        "versions?month=2024-01-01&limit=101",
    ],
)
def test_bad_parameters_are_422_before_sql_access(client: TestClient, suffix: str) -> None:
    with client:
        response = client.get(f"/v1/macro/series/CUSR0000SA0/{suffix}")
    assert response.status_code == 422


def test_openapi_exposes_four_macro_get_routes(client: TestClient) -> None:
    with client:
        paths = client.get("/openapi.json").json()["paths"]
    macro = {path: methods for path, methods in paths.items() if path.startswith("/v1/macro/")}
    assert len(macro) == 4 and all(set(methods) == {"get"} for methods in macro.values())
