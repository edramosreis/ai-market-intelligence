"""Synthetic SDK replies exercise actual perpetual reader queries through HTTP."""

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import QueuePool

from market_intelligence.agent.tools import (
    FUNDING_LATEST,
    FUNDING_SUMMARY,
    OI_LATEST,
    SUMMARY,
    TREASURY_CURVE,
)
from market_intelligence.api import create_app
from market_intelligence.db.hyperliquid_store import HyperliquidStore
from market_intelligence.db.tables import (
    candles,
    funding_events,
    funding_ingestion_runs,
    ingestion_runs,
    open_interest_runs,
    open_interest_snapshots,
    treasury_ingestion_runs,
    treasury_yields,
)
from market_intelligence.db.treasury_store import TreasuryStore
from market_intelligence.hyperliquid.models import (
    FundingEvent,
    OpenInterestSnapshot,
    funding_windows,
)
from market_intelligence.hyperliquid.queries import HyperliquidQueries
from tests.agent_fakes import FakeModel, function, message, response, settings
from tests.integration.test_queries import load as load
from tests.integration.test_queries import reader as reader
from tests.integration.test_treasury_queries import load as load_treasury

pytestmark = pytest.mark.integration
START = datetime(2024, 1, 1, tzinfo=UTC)
END = START + timedelta(hours=4)
NOW = END + timedelta(minutes=10)
RATES = [Decimal("0.0001"), Decimal("-0.00005"), Decimal(0), Decimal("0.0002")]
WINDOW = json.dumps({"start": START.isoformat(), "end": END.isoformat()})


def funding(
    store: HyperliquidStore, rates: Sequence[Decimal] = RATES, *, gap: bool = False
) -> None:
    window = funding_windows(START, END)[0]
    run_id = store.start_funding(window, NOW)
    events = [
        FundingEvent(START + timedelta(hours=index, milliseconds=76), rate, Decimal("-0.000001"))
        for index, rate in enumerate(rates)
        if not gap or index != 1
    ]
    store.persist_funding(run_id, window, events, NOW)


def oi(
    store: HyperliquidStore,
    received: datetime = NOW,
    identity: int = 2,
    value: Decimal = Decimal(0),
) -> None:
    run_id = store.start_open_interest(received - timedelta(seconds=1))
    snapshot = OpenInterestSnapshot(
        UUID(int=identity),
        received - timedelta(seconds=1),
        received,
        value,
        Decimal("42000.125"),
        Decimal("42001.25"),
    )
    store.persist_open_interest(run_id, snapshot, received)


def counts(engine: Engine) -> tuple[int, ...]:
    with engine.connect() as conn:
        return tuple(
            conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
            for table in (
                candles,
                ingestion_runs,
                treasury_yields,
                treasury_ingestion_runs,
                funding_events,
                funding_ingestion_runs,
                open_interest_snapshots,
                open_interest_runs,
            )
        )


@pytest.mark.parametrize(
    "name,path",
    [
        (FUNDING_LATEST, "/v1/hyperliquid/funding/latest"),
        (FUNDING_SUMMARY, "/v1/hyperliquid/funding/summary"),
        (OI_LATEST, "/v1/hyperliquid/open-interest/latest"),
    ],
)
def test_exact_http_reader_evidence_and_no_connections_during_model_waits(
    reader: Engine,
    admin_engine: Engine,
    hyperliquid_store: HyperliquidStore,
    name: str,
    path: str,
) -> None:
    funding(hyperliquid_store)
    oi(hyperliquid_store)
    before = counts(admin_engine)

    def check_pool(index: int, request: dict[str, Any]) -> None:
        assert cast(QueuePool, reader.pool).checkedout() == 0

    model = FakeModel(
        response(function(name, WINDOW if name == FUNDING_SUMMARY else "{}")),
        response(message("Stored Hyperliquid evidence, with native source and units.")),
        before_response=check_pool,
    )
    with model.client() as client:
        app = create_app(
            engine=reader, agent_settings=settings(), model_client=client, now=lambda: NOW
        )
        with TestClient(app) as http:
            reply = http.post(
                "/v1/agent/query", json={"question": "Stored BTC perpetual evidence?"}
            )
            params = (
                {"start": START.isoformat(), "end": END.isoformat()}
                if name == FUNDING_SUMMARY
                else None
            )
            deterministic = http.get(path, params=params).json()
    assert reply.status_code == 200 and reply.json()["status"] == "answered"
    result = reply.json()["evidence"][0]["result"]
    assert result == deterministic
    assert json.loads(model.requests[1]["input"][-1]["output"]) == result
    assert result["instrument"]["denomination_asset"] == "USDT"
    assert (
        result["instrument"]["collateral_asset"]
        == result["instrument"]["settlement_asset"]
        == "USDC"
    )
    if name == FUNDING_SUMMARY:
        assert Decimal(result["rate_sum"]) == Decimal("0.00025")
        assert Decimal(result["rate_sum_percent"]) == Decimal("0.025")
        assert Decimal(result["mean_rate"]) == Decimal("0.0000625")
        assert result["coverage"]["observed_hours"] == 4
    elif name == FUNDING_LATEST:
        assert result["event"]["event_at"] == "2024-01-01T03:00:00.076000Z"
        assert result["event"]["settlement_hour"] == "2024-01-01T03:00:00Z"
        assert result["funding_rate_unit"] == "fraction_per_hour"
    else:
        assert Decimal(result["snapshot"]["open_interest_btc"]) == 0
        assert result["snapshot"]["source_event_at"] is None
        assert result["snapshot"]["received_at"] == NOW.isoformat().replace("+00:00", "Z")
        assert result["quantity_unit"] == "BTC" and result["price_unit"] == "USDT"
        assert result["historical_completeness"] == "not_established"
    assert counts(admin_engine) == before


@pytest.mark.parametrize(
    "condition",
    [
        "funding_empty",
        "summary_empty",
        "oi_empty",
        "summary_gap",
        "funding_stale",
        "oi_stale",
        "funding_zero",
        "funding_negative",
    ],
)
def test_actual_missing_stale_signed_and_zero_data_preserve_native_limitations(
    reader: Engine,
    admin_engine: Engine,
    hyperliquid_store: HyperliquidStore,
    condition: str,
) -> None:
    name: str = FUNDING_LATEST
    args, code, current = "{}", "no_data", NOW
    if condition.startswith("summary"):
        name, args = FUNDING_SUMMARY, WINDOW
        if condition == "summary_gap":
            funding(hyperliquid_store, gap=True)
            code = "incomplete"
    elif condition.startswith("oi"):
        name = OI_LATEST
        if condition == "oi_stale":
            oi(hyperliquid_store)
            current, code = NOW + timedelta(hours=2), "stale"
    elif condition == "funding_stale":
        funding(hyperliquid_store)
        current, code = NOW + timedelta(hours=3), "stale"
    elif condition in ("funding_zero", "funding_negative"):
        funding(
            hyperliquid_store,
            [Decimal(0)] * 4 if condition == "funding_zero" else [Decimal("-0.00005")] * 4,
        )
        code = ""
    before = counts(admin_engine)
    model = FakeModel(
        response(function(name, args)), response(message("Native signed funding evidence."))
    )
    with model.client() as client:
        app = create_app(
            engine=reader, agent_settings=settings(), model_client=client, now=lambda: current
        )
        with TestClient(app) as http:
            reply = http.post("/v1/agent/query", json={"question": "Current funding or OI?"})
    body = reply.json()
    assert reply.status_code == 200 and body["limitations"] == ([code] if code else [])
    assert len(body["evidence"]) == 1 and len(model.requests) == (1 if code else 2)
    evidence = body["evidence"][0]["result"]
    if code:
        assert "Hyperliquid" in body["answer"] and "Coinbase" not in body["answer"]
    else:
        expected = Decimal(0) if condition == "funding_zero" else Decimal("-0.00005")
        assert Decimal(evidence["event"]["funding_rate"]) == expected
    if condition == "summary_gap":
        assert evidence["coverage"]["missing_hours"] == 1
        assert evidence["coverage"]["missing_ranges"][0]["start"] == "2024-01-01T01:00:00Z"
        assert evidence["rate_sum"] is evidence["rate_sum_percent"] is evidence["mean_rate"] is None
    assert counts(admin_engine) == before


def test_offset_windows_preserve_arguments_and_normalize_only_the_requested_instants(
    reader: Engine,
    hyperliquid_store: HyperliquidStore,
) -> None:
    funding(hyperliquid_store)
    arguments = {"start": "2024-01-01T01:00:00+01:00", "end": "2024-01-01T05:00:00+01:00"}
    model = FakeModel(
        response(function(FUNDING_SUMMARY, json.dumps(arguments))),
        response(message("Settled funding sum.")),
    )
    with (
        model.client() as client,
        TestClient(
            create_app(
                engine=reader, agent_settings=settings(), model_client=client, now=lambda: NOW
            )
        ) as http,
    ):
        body = http.post(
            "/v1/agent/query", json={"question": "Funding in these exact instants?"}
        ).json()
    assert body["status"] == "answered"
    assert body["evidence"][0]["arguments"] == arguments
    assert body["evidence"][0]["result"]["start"] == "2024-01-01T00:00:00Z"
    assert body["evidence"][0]["result"]["end"] == "2024-01-01T04:00:00Z"


def test_three_sources_share_existing_budgets_and_keep_separate_evidence(
    reader: Engine,
    admin_engine: Engine,
    hyperliquid_store: HyperliquidStore,
    treasury_store: TreasuryStore,
    load: Callable[[Sequence[int]], None],
) -> None:
    load([0, 1, 2])
    funding(hyperliquid_store)
    load_treasury(treasury_store)
    before = counts(admin_engine)
    model = FakeModel(
        response(
            function(SUMMARY, '{"start":"2024-01-01","end":"2024-01-01T00:15:00Z"}', "call_1")
        ),
        response(function(TREASURY_CURVE, '{"observed_on":"2024-01-02"}', "call_2")),
        response(function(FUNDING_SUMMARY, WINDOW, "call_3")),
        response(message("Separate Coinbase, Treasury and Hyperliquid evidence.")),
    )
    with (
        model.client() as client,
        TestClient(
            create_app(
                engine=reader,
                agent_settings=settings(),
                model_client=client,
                now=lambda: START + timedelta(days=3),
            )
        ) as http,
    ):
        body = http.post(
            "/v1/agent/query", json={"question": "Report these three sources separately."}
        ).json()
    assert body["status"] == "answered" and len(body["evidence"]) == 3
    assert body["model_requests"] == 4 and body["tool_calls"] == 3
    assert model.requests[-1]["tool_choice"] == "none"
    assert [item["name"] for item in body["evidence"]] == [SUMMARY, TREASURY_CURVE, FUNDING_SUMMARY]
    for index in range(1, 4):
        assert (
            json.loads(model.requests[index]["input"][-1]["output"])
            == body["evidence"][index - 1]["result"]
        )
    assert counts(admin_engine) == before


@pytest.mark.parametrize("name", [FUNDING_SUMMARY, OI_LATEST])
def test_evidence_stays_exact_when_later_facts_change_during_model_waits(
    reader: Engine,
    hyperliquid_store: HyperliquidStore,
    name: str,
) -> None:
    funding(hyperliquid_store)
    oi(hyperliquid_store)
    reads = HyperliquidQueries(reader, now=lambda: NOW)
    initial = (
        reads.funding_summary(START, END)
        if name == FUNDING_SUMMARY
        else reads.latest_open_interest()
    )

    def change(index: int, request: dict[str, Any]) -> None:
        assert cast(QueuePool, reader.pool).checkedout() == 0
        if index == 2:
            if name == FUNDING_SUMMARY:
                funding(hyperliquid_store, [rate * 2 for rate in RATES])
            else:
                oi(hyperliquid_store, identity=4, value=Decimal(10))

    model = FakeModel(
        response(function(name, WINDOW if name == FUNDING_SUMMARY else "{}")),
        response(message("Evidence from the completed reader snapshot.")),
        before_response=change,
    )
    with (
        model.client() as client,
        TestClient(
            create_app(
                engine=reader, agent_settings=settings(), model_client=client, now=lambda: NOW
            )
        ) as http,
    ):
        body = http.post("/v1/agent/query", json={"question": "Stored funding or OI?"}).json()
    assert body["status"] == "answered"
    assert body["evidence"][0]["result"] == initial.model_dump(mode="json")
    assert json.loads(model.requests[1]["input"][-1]["output"]) == initial.model_dump(mode="json")
    current = (
        reads.funding_summary(START, END)
        if name == FUNDING_SUMMARY
        else reads.latest_open_interest()
    )
    assert current != initial


@pytest.mark.parametrize("name", [FUNDING_LATEST, FUNDING_SUMMARY, OI_LATEST])
def test_real_reader_sql_failure_returns_sanitized_503_and_releases_connections(
    reader: Engine, name: str
) -> None:
    def fail(*args: Any) -> None:
        raise OperationalError("private SQL", {}, RuntimeError("secret credentials"))

    sa.event.listen(reader, "before_cursor_execute", fail)
    try:
        model = FakeModel(response(function(name, WINDOW if name == FUNDING_SUMMARY else "{}")))
        with (
            model.client() as client,
            TestClient(
                create_app(
                    engine=reader, agent_settings=settings(), model_client=client, now=lambda: NOW
                )
            ) as http,
        ):
            reply = http.post("/v1/agent/query", json={"question": "Stored funding/OI?"})
        assert reply.status_code == 503 and reply.json()["limitations"] == ["database_unavailable"]
        assert not reply.json()["evidence"] and len(model.requests) == 1
        assert "secret" not in reply.text and "private SQL" not in reply.text
        assert cast(QueuePool, reader.pool).checkedout() == 0
    finally:
        sa.event.remove(reader, "before_cursor_execute", fail)


def test_large_gap_evidence_is_omitted_before_output_or_model_relay(
    reader: Engine,
    hyperliquid_store: HyperliquidStore,
) -> None:
    end = START + timedelta(hours=130)
    window = funding_windows(START, end)[0]
    run_id = hyperliquid_store.start_funding(window, end)
    hyperliquid_store.persist_funding(
        run_id,
        window,
        [
            FundingEvent(START + timedelta(hours=index, milliseconds=76), Decimal(0), Decimal(0))
            for index in range(1, 130, 2)
        ],
        end,
    )
    model = FakeModel(
        response(
            function(
                FUNDING_SUMMARY, json.dumps({"start": START.isoformat(), "end": end.isoformat()})
            )
        )
    )
    with (
        model.client() as client,
        TestClient(
            create_app(
                engine=reader,
                agent_settings=settings(max_tool_output_bytes=1024),
                model_client=client,
                now=lambda: end,
            )
        ) as http,
    ):
        reply = http.post("/v1/agent/query", json={"question": "Full funding window?"})
    assert reply.status_code == 200 and reply.json()["limitations"] == ["output_limit"]
    assert not reply.json()["evidence"] and len(model.requests) == 1
    assert len(reply.content) < 1024
