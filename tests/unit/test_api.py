from typing import cast
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from market_intelligence.api import create_app, get_queries
from market_intelligence.config import AgentSettings, ApiSettings
from market_intelligence.queries.models import UnknownMarketError
from market_intelligence.queries.service import MarketQueries


@pytest.fixture
def client() -> TestClient:
    app = create_app(
        engine=cast(Engine, object()),
        api_settings=ApiSettings(_env_file=None),  # type: ignore[call-arg]
        agent_settings=AgentSettings(_env_file=None),  # type: ignore[call-arg]
    )
    return TestClient(app)


def test_liveness_and_openapi_require_no_database(client: TestClient) -> None:
    with client:
        assert client.get("/health/live").json() == {"status": "alive"}
        schema = client.get("/openapi.json").json()
        assert "/v1/markets/{market_id}/summary" in schema["paths"]
        assert "/v1/agent/query" in schema["paths"]


@pytest.mark.parametrize("path", ["/health/ready", "/v1/markets", "/v1/markets/1/latest"])
def test_database_failure_returns_sanitized_503(client: TestClient, path: str) -> None:
    queries = Mock(spec=MarketQueries)
    failure = OperationalError("secret query", {"password": "sensitive"}, Exception("private"))
    queries.ready.side_effect = failure
    queries.list_markets.side_effect = failure
    queries.latest.side_effect = failure
    cast(FastAPI, client.app).dependency_overrides[get_queries] = lambda: queries
    with client:
        response = client.get(path)
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}


def test_readiness_requires_schema_match(client: TestClient) -> None:
    queries = Mock(spec=MarketQueries)
    queries.ready.return_value = False
    cast(FastAPI, client.app).dependency_overrides[get_queries] = lambda: queries
    with client:
        assert client.get("/health/ready").status_code == 503


def test_unknown_market_returns_404(client: TestClient) -> None:
    queries = Mock(spec=MarketQueries)
    queries.latest.side_effect = UnknownMarketError()
    cast(FastAPI, client.app).dependency_overrides[get_queries] = lambda: queries
    with client:
        assert client.get("/v1/markets/999/latest").status_code == 404


@pytest.mark.parametrize(
    "query",
    [
        {"start": "2024-01-01", "end": "2024-01-02", "limit": "secret-invalid"},
        {"start": "2024-01-01", "end": "2024-01-02", "limit": "501"},
        {"start": "2024-01-01T00:00:00", "end": "2024-01-02"},
        {"start": "private-invalid-time", "end": "2024-01-02"},
        {"end": "2024-01-02"},
    ],
)
def test_invalid_request_does_not_echo_inputs(client: TestClient, query: dict[str, str]) -> None:
    with client:
        response = client.get("/v1/markets/1/candles", params=query)
    assert response.status_code == 422
    assert "secret-invalid" not in response.text and "private-invalid-time" not in response.text
