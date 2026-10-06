"""Simulated model calls execute real SELECT-only PostgreSQL queries through HTTP."""

from collections.abc import Callable, Sequence
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.pool import QueuePool

from market_intelligence.agent.tools import LATEST, SUMMARY
from market_intelligence.api import create_app
from market_intelligence.config import ApiSettings
from market_intelligence.db.tables import candles, ingestion_runs
from tests.agent_fakes import END, FakeModel, function, message, response, settings
from tests.integration.test_queries import NOW
from tests.integration.test_queries import load as load
from tests.integration.test_queries import reader as reader

pytestmark = pytest.mark.integration
WINDOW = '{"start":"2024-01-01T00:00:00Z","end":"2024-01-01T00:15:00Z"}'


def counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as conn:
        return (
            conn.execute(sa.select(sa.func.count()).select_from(candles)).scalar_one(),
            conn.execute(sa.select(sa.func.count()).select_from(ingestion_runs)).scalar_one(),
        )


def test_agent_summary_matches_stored_facts_without_writes_or_open_model_wait_transaction(
    reader: Engine, admin_engine: Engine, load: Callable[[Sequence[int]], None]
) -> None:
    load([0, 1, 2])
    before = counts(admin_engine)

    def no_open_transaction(index: int, request: dict[str, Any]) -> None:
        assert cast(QueuePool, reader.pool).checkedout() == 0
        if index == 2:
            assert request["input"][-1]["type"] == "function_call_output"

    model = FakeModel(
        response(function(SUMMARY, WINDOW)),
        response(message()),
        before_response=no_open_transaction,
    )
    with model.client() as client:
        app = create_app(
            engine=reader,
            agent_settings=settings(),
            model_client=client,
            now=lambda: NOW,
            api_settings=ApiSettings(_env_file=None),  # type: ignore[call-arg]
        )
        with TestClient(app) as http:
            reply = http.post(
                "/v1/agent/query", json={"question": "BTC return Jan 1, 00:00-00:15 UTC?"}
            )
            deterministic = http.get(
                "/v1/markets/1/summary",
                params={
                    "start": "2024-01-01",
                    "end": END.isoformat(),
                },
            )
    assert reply.status_code == 200 and reply.json()["status"] == "answered"
    evidence = reply.json()["evidence"][0]["result"]
    assert evidence == deterministic.json()
    assert evidence["opening_price"] == "100.000000000000000000"
    assert evidence["closing_price"] == "105.000000000000000000"
    assert evidence["open_to_close_return_percent"] == "5.00000000"
    assert evidence["high"] == "111.000000000000000000"
    assert evidence["low"] == "94.000000000000000000"
    assert evidence["base_volume"] == "0.600000000000000000"
    assert evidence["provenance"]["ingestion_run_count"] == 1
    assert counts(admin_engine) == before


@pytest.mark.parametrize("condition", ["empty", "gap", "stale", "unaligned", "sql"])
def test_agent_reports_real_data_limitations_and_never_writes(
    reader: Engine, admin_engine: Engine, load: Callable[[Sequence[int]], None], condition: str
) -> None:
    if condition != "empty":
        load([0, 2] if condition == "gap" else [0, 1, 2])
    name: str = SUMMARY
    arguments = WINDOW
    if condition == "stale":
        name, arguments = LATEST, "{}"
    elif condition == "unaligned":
        arguments = '{"start":"2024-01-01T00:16:00Z","end":"2024-01-01T00:31:00Z"}'
    elif condition == "sql":
        name = "execute_sql"
    before = counts(admin_engine)
    model = FakeModel(response(function(name, arguments)))
    with model.client() as client:
        app = create_app(
            engine=reader, agent_settings=settings(), model_client=client, now=lambda: NOW
        )
        with TestClient(app) as http:
            reply = http.post("/v1/agent/query", json={"question": "Stored BTC data?"})
    assert reply.status_code == 200 and reply.json()["status"] == "limited"
    expected = {
        "empty": "no_data",
        "gap": "incomplete",
        "stale": "stale",
        "unaligned": "invalid_arguments",
        "sql": "unknown_tool",
    }[condition]
    assert reply.json()["limitations"] == [expected]
    assert len(model.requests) == 1 and counts(admin_engine) == before
    if condition == "gap":
        evidence = reply.json()["evidence"][0]["result"]
        assert evidence["coverage"]["missing_buckets"] == 1
        assert evidence["high"] is None and evidence["open_to_close_return_percent"] is None
