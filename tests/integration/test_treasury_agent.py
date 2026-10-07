"""Simulated model selection reaches real reader-role Treasury queries through HTTP."""

import json
from collections.abc import Callable, Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.pool import QueuePool

from market_intelligence.agent.tools import SUMMARY, TREASURY_CURVE, TREASURY_SPREADS
from market_intelligence.api import create_app
from market_intelligence.db.tables import (
    candles,
    ingestion_runs,
    treasury_ingestion_runs,
    treasury_yields,
)
from market_intelligence.db.treasury_store import TreasuryStore
from market_intelligence.treasury.queries import TreasuryQueries
from market_intelligence.treasury.service import ingest_treasury
from tests.agent_fakes import FakeModel, function, message, response, settings
from tests.integration.test_queries import load as load
from tests.integration.test_queries import reader as reader
from tests.integration.test_treasury_ingestion import END, NOW, START, source_with
from tests.integration.test_treasury_queries import load as load_treasury
from tests.unit.test_treasury import entry, feed

pytestmark = pytest.mark.integration
CURVE = '{"observed_on":"2024-01-02"}'
HISTORY = '{"start":"2024-01-01","end":"2024-02-01","cursor":null}'


def counts(engine: Engine) -> tuple[int, ...]:
    with engine.connect() as conn:
        return tuple(
            conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
            for table in (candles, ingestion_runs, treasury_yields, treasury_ingestion_runs)
        )


@pytest.mark.parametrize("name,arguments", [(TREASURY_CURVE, CURVE), (TREASURY_SPREADS, HISTORY)])
def test_treasury_agent_evidence_matches_reader_queries_without_writes_or_model_wait_transactions(
    reader: Engine, admin_engine: Engine, treasury_store: TreasuryStore, name: str, arguments: str
) -> None:
    load_treasury(treasury_store)
    before = counts(admin_engine)

    def no_open_transaction(index: int, request: dict[str, Any]) -> None:
        assert cast(QueuePool, reader.pool).checkedout() == 0

    model = FakeModel(
        response(function(name, arguments)),
        response(
            message("US Treasury 10Y-minus-2Y spread is -0.125 percentage points / -12.5 bps.")
        ),
        before_response=no_open_transaction,
    )
    with model.client() as client:
        app = create_app(
            engine=reader, agent_settings=settings(), model_client=client, now=lambda: NOW
        )
        with TestClient(app) as http:
            reply = http.post(
                "/v1/agent/query", json={"question": "Treasury yields/spread for January 2024?"}
            )
            if name == TREASURY_CURVE:
                deterministic = http.get(
                    "/v1/treasury/curve", params={"observed_on": "2024-01-02"}
                ).json()
            else:
                deterministic = (
                    TreasuryQueries(reader, now=lambda: NOW)
                    .spread_page(START, END)
                    .model_dump(mode="json")
                )
    assert reply.status_code == 200 and reply.json()["status"] == "answered"
    evidence = reply.json()["evidence"][0]["result"]
    assert evidence == deterministic
    assert json.loads(model.requests[1]["input"][-1]["output"]) == evidence
    curve = evidence["curve"] if name == TREASURY_CURVE else evidence["observations"][0]
    assert curve["spread"]["percentage_points"] == "-0.125000000000000000"
    assert curve["spread"]["basis_points"] == "-12.500000000000000000"
    assert counts(admin_engine) == before


@pytest.mark.parametrize(
    "condition",
    ["empty_curve", "empty_history", "date_only", "missing_benchmarks", "zero", "missing_row"],
)
def test_treasury_agent_preserves_real_missingness_zero_and_source_date_semantics(
    reader: Engine, admin_engine: Engine, treasury_store: TreasuryStore, condition: str
) -> None:
    fields = {
        "date_only": "",
        "missing_benchmarks": '<d:BC_2YEAR m:null="true"/>',
        "zero": "<d:BC_2YEAR>0</d:BC_2YEAR><d:BC_10YEAR>0.25</d:BC_10YEAR>",
        "missing_row": "<d:BC_2YEAR>4.25</d:BC_2YEAR><d:BC_10YEAR>4.125</d:BC_10YEAR>",
    }
    if condition in fields:
        with source_with(
            lambda _: httpx.Response(200, content=feed(entry("2024-01-02", fields[condition])))
        ) as source:
            ingest_treasury(source, treasury_store, START, END, now=lambda: NOW)
    if condition == "missing_row":
        with admin_engine.begin() as conn:
            conn.execute(treasury_yields.delete().where(treasury_yields.c.tenor == "2Y"))
    before = counts(admin_engine)
    curve_tool = condition in ("empty_curve", "date_only", "missing_row")
    model = FakeModel(
        response(
            function(
                TREASURY_CURVE if curve_tool else TREASURY_SPREADS, CURVE if curve_tool else HISTORY
            )
        ),
        response(message("The supplied Treasury spread is +0.25 percentage points / +25 bps.")),
    )
    with model.client() as client:
        app = create_app(
            engine=reader, agent_settings=settings(), model_client=client, now=lambda: NOW
        )
        with TestClient(app) as http:
            reply = http.post("/v1/agent/query", json={"question": "Stored Treasury evidence?"})
    body = reply.json()
    assert reply.status_code == 200 and counts(admin_engine) == before
    expected = {
        "empty_curve": "no_data",
        "empty_history": "no_data",
        "date_only": "missing_rates",
        "missing_benchmarks": "missing_rates",
        "missing_row": "incomplete",
    }
    assert body["limitations"] == ([] if condition == "zero" else [expected[condition]])
    assert len(model.requests) == (2 if condition == "zero" else 1)
    evidence = body["evidence"][0]["result"]
    if condition.startswith("empty"):
        assert "Treasury" in body["answer"] and "Coinbase" not in body["answer"]
        assert "publication-calendar" in body["answer"]
    elif condition == "date_only":
        curve = evidence["curve"]
        assert curve["stored_rates"] == 14 and curve["available_rates"] == 0
        assert {rate["missing_reason"] for rate in curve["rates"]} == {"field_absent"}
    elif condition == "missing_benchmarks":
        day = evidence["observations"][0]
        assert day["two_year"]["missing_reason"] == "source_null"
        assert day["ten_year"]["missing_reason"] == "field_absent"
        assert day["spread"]["basis_points"] is None
    elif condition == "missing_row":
        assert evidence["curve"]["status"] == "incomplete_stored_curve"
        assert "not_stored" in {rate["missing_reason"] for rate in evidence["curve"]["rates"]}
    else:
        zero = evidence["observations"][0]["two_year"]["yield_percent"]
        assert isinstance(zero, str) and Decimal(zero) == 0
        assert evidence["observations"][0]["spread"]["basis_points"] == "25.000000000000000000"


@pytest.mark.parametrize("follow_cursor", [False, True])
def test_real_history_pagination_cannot_be_presented_as_complete_without_continuation(
    reader: Engine, treasury_store: TreasuryStore, follow_cursor: bool
) -> None:
    payload = feed(
        "".join(
            entry(
                (START + timedelta(days=offset)).isoformat(),
                "<d:BC_2YEAR>4.25</d:BC_2YEAR><d:BC_10YEAR>4.125</d:BC_10YEAR>",
            )
            for offset in range(21)
        )
    )
    with source_with(lambda _: httpx.Response(200, content=payload)) as source:
        ingest_treasury(source, treasury_store, START, END, now=lambda: NOW)
    first = TreasuryQueries(reader, now=lambda: NOW).spread_page(START, END)
    replies = [response(function(TREASURY_SPREADS, HISTORY))]
    if follow_cursor:
        replies.append(
            response(
                function(
                    TREASURY_SPREADS,
                    json.dumps(
                        {"start": "2024-01-01", "end": "2024-02-01", "cursor": first.next_cursor}
                    ),
                    "call_2",
                )
            )
        )
    replies.append(
        response(message("The requested stored source-date spreads have been retrieved."))
    )
    model = FakeModel(*replies)
    with model.client() as client:
        app = create_app(
            engine=reader, agent_settings=settings(), model_client=client, now=lambda: NOW
        )
        with TestClient(app) as http:
            reply = http.post(
                "/v1/agent/query", json={"question": "January 2024 Treasury spread history?"}
            )
    body = reply.json()
    assert reply.status_code == 200
    assert body["status"] == ("answered" if follow_cursor else "limited")
    assert body["limitations"] == ([] if follow_cursor else ["partial_results"])
    assert body["evidence"][0]["result"]["coverage"]["observed_dates"] == 21
    assert len(body["evidence"][0]["result"]["observations"]) == 20
    if follow_cursor:
        assert body["evidence"][1]["arguments"]["cursor"] == first.next_cursor
        assert len(body["evidence"][1]["result"]["observations"]) == 1
        assert body["evidence"][1]["result"]["next_cursor"] is None


def test_mixed_domain_agent_queries_use_one_reader_without_writes(
    reader: Engine,
    admin_engine: Engine,
    treasury_store: TreasuryStore,
    load: Callable[[Sequence[int]], None],
) -> None:
    load([0, 1, 2])
    load_treasury(treasury_store)
    before = counts(admin_engine)
    model = FakeModel(
        response(function(SUMMARY, '{"start":"2024-01-01","end":"2024-01-01T00:15:00Z"}')),
        response(function(TREASURY_CURVE, CURVE, "call_2")),
        response(message("Coinbase BTC/USD and US Treasury have separate source/date evidence.")),
        before_response=lambda *_: assert_reader_released(reader),
    )
    with model.client() as client:
        app = create_app(
            engine=reader, agent_settings=settings(), model_client=client, now=lambda: NOW
        )
        with TestClient(app) as http:
            reply = http.post(
                "/v1/agent/query",
                json={"question": "Report these BTC and Treasury observations separately."},
            )
    body = reply.json()
    assert reply.status_code == 200 and body["status"] == "answered"
    assert body["model_requests"] == 3 and body["tool_calls"] == 2
    assert body["evidence"][0]["result"]["market"]["source_code"] == "coinbase_exchange"
    assert body["evidence"][1]["result"]["source_code"] == "us_treasury"
    assert counts(admin_engine) == before


def assert_reader_released(reader: Engine) -> None:
    assert cast(QueuePool, reader.pool).checkedout() == 0


def test_collected_treasury_evidence_survives_correction_during_model_wait(
    reader: Engine, admin_engine: Engine, treasury_store: TreasuryStore
) -> None:
    load_treasury(treasury_store)

    def correct_during_model_wait(index: int, request: dict[str, Any]) -> None:
        assert_reader_released(reader)
        if index == 2:
            with admin_engine.begin() as conn:
                conn.execute(
                    treasury_yields.update()
                    .where(treasury_yields.c.tenor == "2Y")
                    .values(yield_percent=8)
                )

    model = FakeModel(
        response(function(TREASURY_CURVE, CURVE)),
        response(message("The retrieved spread was -0.125 percentage points.")),
        before_response=correct_during_model_wait,
    )
    with model.client() as client:
        app = create_app(
            engine=reader, agent_settings=settings(), model_client=client, now=lambda: NOW
        )
        with TestClient(app) as http:
            reply = http.post(
                "/v1/agent/query", json={"question": "Treasury spread on January 2, 2024?"}
            )
    evidence = reply.json()["evidence"][0]["result"]
    assert reply.status_code == 200 and reply.json()["status"] == "answered"
    assert evidence["curve"]["spread"]["percentage_points"] == "-0.125000000000000000"
    assert json.loads(model.requests[1]["input"][-1]["output"]) == evidence
    current = TreasuryQueries(reader, now=lambda: NOW).curve(START + timedelta(days=1))
    assert current.curve.spread.percentage_points == Decimal("-3.875")
