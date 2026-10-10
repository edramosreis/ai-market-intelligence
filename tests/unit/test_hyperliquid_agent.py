"""Strict native perpetual tools and bounded exact evidence through the real SDK."""

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from market_intelligence.agent.instructions import instructions
from market_intelligence.agent.models import LimitationCode
from market_intelligence.agent.runner import AgentRunner
from market_intelligence.agent.tools import (
    FUNDING_LATEST,
    FUNDING_SUMMARY,
    OI_LATEST,
    TREASURY_CURVE,
    MarketTools,
)
from market_intelligence.config import ApiSettings
from market_intelligence.hyperliquid.queries import HyperliquidQueries
from market_intelligence.hyperliquid.query_models import (
    FundingCoverage,
    FundingLatest,
    FundingObservation,
    FundingProvenance,
    FundingSummary,
    OpenInterestLatest,
    OpenInterestObservation,
    PerpetualInstrument,
)
from tests.agent_fakes import (
    START,
    FakeModel,
    function,
    hyperliquid_queries,
    macro_queries,
    message,
    queries,
    response,
    settings,
    treasury_queries,
)

END = START + timedelta(hours=4)
NOW = END + timedelta(minutes=10)
WINDOW = json.dumps({"start": START.isoformat(), "end": END.isoformat()})


def funding_latest() -> FundingLatest:
    return FundingLatest(
        instrument=PerpetualInstrument(),
        retrieved_at=NOW,
        status="fresh",
        event=FundingObservation(
            event_at=END - timedelta(hours=1) + timedelta(milliseconds=76),
            settlement_hour=END - timedelta(hours=1),
            funding_rate=Decimal("-0.00005"),
            premium=Decimal("-0.000001"),
            provenance=FundingProvenance(
                first_ingested_at=NOW, last_updated_at=NOW, last_ingestion_run_id=UUID(int=1)
            ),
        ),
        age_seconds=Decimal("4199.924"),
        stale_after_seconds=7200,
    )


def funding_summary() -> FundingSummary:
    return FundingSummary(
        instrument=PerpetualInstrument(),
        retrieved_at=NOW,
        start=START,
        end=END,
        coverage=FundingCoverage(
            status="complete",
            expected_hours=4,
            observed_hours=4,
            missing_hours=0,
            first_event_at=START + timedelta(milliseconds=76),
            last_event_at=END - timedelta(hours=1) + timedelta(milliseconds=76),
            missing_ranges=[],
            missing_range_count=0,
            missing_ranges_truncated=False,
        ),
        rate_sum=Decimal("0.00025"),
        rate_sum_percent=Decimal("0.02500"),
        mean_rate=Decimal("0.000062500000000000"),
    )


def oi_latest() -> OpenInterestLatest:
    return OpenInterestLatest(
        instrument=PerpetualInstrument(),
        retrieved_at=NOW,
        status="fresh",
        snapshot=OpenInterestObservation(
            snapshot_id=UUID(int=2),
            fetch_started_at=NOW - timedelta(seconds=61),
            received_at=NOW - timedelta(seconds=60),
            open_interest_btc=Decimal(0),
            mark_price_usdt=Decimal("42000.125"),
            oracle_price_usdt=Decimal("42001.25"),
            ingestion_run_id=UUID(int=3),
        ),
        age_seconds=Decimal(60),
        stale_after_seconds=3600,
    )


def data() -> Mock:
    result = hyperliquid_queries()
    result.latest_funding.return_value = funding_latest()
    result.funding_summary.return_value = funding_summary()
    result.latest_open_interest.return_value = oi_latest()
    return result


@pytest.mark.parametrize(
    "name,arguments",
    [
        (FUNDING_LATEST, "{}"),
        (FUNDING_SUMMARY, WINDOW),
        (OI_LATEST, "{}"),
    ],
)
def test_tools_relay_exact_native_evidence_without_using_other_domains(
    name: str,
    arguments: str,
) -> None:
    btc, treasury, perpetual = queries(), treasury_queries(), data()
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
    model = FakeModel(
        response(reasoning, function(name, arguments)),
        response(message("Stored Hyperliquid evidence.")),
    )
    with model.client() as client:
        result = AgentRunner(
            client,
            MarketTools(btc, treasury, perpetual, macro_queries()),
            settings(),
            now=lambda: NOW,
        ).run("Stored perpetual evidence?")
    assert result.status == "answered" and result.limitations == []
    assert not btc.mock_calls and not treasury.mock_calls
    expected: FundingSummary | FundingLatest | OpenInterestLatest
    if name == FUNDING_SUMMARY:
        perpetual.funding_summary.assert_called_once_with(START, END)
        expected = funding_summary()
    elif name == FUNDING_LATEST:
        perpetual.latest_funding.assert_called_once_with()
        expected = funding_latest()
    else:
        perpetual.latest_open_interest.assert_called_once_with()
        expected = oi_latest()
    assert result.evidence[0].result == expected
    assert json.loads(model.requests[1]["input"][-1]["output"]) == expected.model_dump(mode="json")
    assert model.requests[1]["input"][1]["encrypted_content"] == "opaque"
    assert result.model_requests == 2 and result.tool_calls == 1


@pytest.mark.parametrize(
    "name,arguments",
    [
        (FUNDING_LATEST, '{"coin":"ETH"}'),
        (OI_LATEST, '{"observed_on":"2024-01-01"}'),
        (OI_LATEST, '{"sql":"SELECT 1"}'),
        (OI_LATEST, "[]"),
        (FUNDING_SUMMARY, '{"start":1,"end":"2024-01-02"}'),
        (FUNDING_SUMMARY, '{"start":"2024-01-01"}'),
        (FUNDING_SUMMARY, '{"start":"2024-01-01","end":"2024-01-02","coin":"ETH"}'),
        (FUNDING_SUMMARY, '{"start":"2024-01-01","start":"2024-01-02","end":"2024-01-03"}'),
        (FUNDING_SUMMARY, '{"start":"2024-01-01T00:00:00","end":"2024-01-02"}'),
    ],
)
def test_invalid_arguments_stop_before_reader_execution(name: str, arguments: str) -> None:
    perpetual = data()
    model = FakeModel(response(function(name, arguments)))
    with model.client() as client:
        result = AgentRunner(
            client,
            MarketTools(queries(), treasury_queries(), perpetual, macro_queries()),
            settings(),
            now=lambda: NOW,
        ).run("Funding?")
    assert result.limitations == [LimitationCode.INVALID_ARGUMENTS]
    assert not perpetual.mock_calls and not result.evidence and len(model.requests) == 1


@pytest.mark.parametrize(
    "start,end",
    [
        ("2023-12-31", "2024-01-01"),
        ("2024-01-01T00:05:00Z", "2024-01-01T01:00:00Z"),
        ("2024-01-01T00:00:00Z", "2024-01-01T01:30:00Z"),
        ("2024-01-02", "2024-01-01"),
        ("2024-01-01", "2024-01-01T05:00:00Z"),
        ("2024-01-01", "2024-01-03"),
    ],
)
def test_funding_bounds_are_validated_by_shared_reader_before_database_access(
    start: str, end: str
) -> None:
    engine = create_engine("postgresql+psycopg://unused:unused@invalid/unused")
    try:
        api_settings = ApiSettings(_env_file=None, max_window_days=1)  # type: ignore[call-arg]
        perpetual = HyperliquidQueries(engine, api_settings, now=lambda: NOW)
        model = FakeModel(
            response(function(FUNDING_SUMMARY, json.dumps({"start": start, "end": end})))
        )
        with model.client() as client:
            result = AgentRunner(
                client,
                MarketTools(queries(), treasury_queries(), perpetual, macro_queries()),
                settings(),
                now=lambda: NOW,
            ).run("Funding?")
        assert result.limitations == [LimitationCode.INVALID_ARGUMENTS] and not result.evidence
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "condition",
    ["funding_empty", "funding_stale", "summary_empty", "summary_gap", "oi_empty", "oi_stale"],
)
def test_native_limitations_retain_evidence_and_stop_further_model_requests(condition: str) -> None:
    perpetual = data()
    code = LimitationCode.NO_DATA
    name: str
    if condition.startswith("funding"):
        name, args = FUNDING_LATEST, "{}"
        changed: dict[str, Any] = {"status": "no_data", "event": None, "age_seconds": None}
        if condition == "funding_stale":
            code, changed = LimitationCode.STALE, {"status": "stale", "age_seconds": Decimal(8000)}
        perpetual.latest_funding.return_value = funding_latest().model_copy(update=changed)
    elif condition.startswith("oi"):
        name, args = OI_LATEST, "{}"
        changed = {"status": "no_data", "snapshot": None, "age_seconds": None}
        if condition == "oi_stale":
            code, changed = LimitationCode.STALE, {"status": "stale", "age_seconds": Decimal(4000)}
        perpetual.latest_open_interest.return_value = oi_latest().model_copy(update=changed)
    else:
        name, args = FUNDING_SUMMARY, WINDOW
        value = funding_summary()
        status, observed = "no_data", 0
        if condition == "summary_gap":
            code, status, observed = LimitationCode.INCOMPLETE, "incomplete", 3
        perpetual.funding_summary.return_value = value.model_copy(
            update={
                "coverage": value.coverage.model_copy(
                    update={
                        "status": status,
                        "observed_hours": observed,
                        "missing_hours": 4 - observed,
                    }
                ),
                "rate_sum": None,
                "rate_sum_percent": None,
                "mean_rate": None,
            }
        )
    model = FakeModel(response(function(name, args)))
    with model.client() as client:
        result = AgentRunner(
            client,
            MarketTools(queries(), treasury_queries(), perpetual, macro_queries()),
            settings(),
            now=lambda: NOW,
        ).run("Current funding or OI?")
    assert result.limitations == [code] and result.status == "limited"
    assert len(result.evidence) == 1 and result.model_requests == 1
    assert (
        "Hyperliquid" in result.answer
        and "Coinbase" not in result.answer
        and "Treasury" not in result.answer
    )
    assert ("open-interest" in result.answer) == condition.startswith("oi")


def test_three_domain_tools_share_existing_call_and_request_budget() -> None:
    model = FakeModel(
        response(function(FUNDING_SUMMARY, WINDOW, "call_1")),
        response(function(OI_LATEST, "{}", "call_2")),
        response(function(TREASURY_CURVE, '{"observed_on":"2024-01-01"}', "call_3")),
        response(message("Separate source evidence.")),
    )
    with model.client() as client:
        result = AgentRunner(
            client,
            MarketTools(queries(), treasury_queries(), data(), macro_queries()),
            settings(),
            now=lambda: NOW,
        ).run("Report sources separately.")
    assert result.status == "answered" and len(result.evidence) == 3
    assert result.model_requests == 4 and result.tool_calls == 3
    assert model.requests[-1]["tool_choice"] == "none"


@pytest.mark.parametrize("name", [FUNDING_LATEST, FUNDING_SUMMARY, OI_LATEST])
def test_database_failures_are_sanitized(name: str) -> None:
    perpetual = data()
    method = {
        FUNDING_LATEST: perpetual.latest_funding,
        FUNDING_SUMMARY: perpetual.funding_summary,
        OI_LATEST: perpetual.latest_open_interest,
    }[name]
    method.side_effect = OperationalError("private SQL", {}, RuntimeError("secret"))
    model = FakeModel(response(function(name, WINDOW if name == FUNDING_SUMMARY else "{}")))
    with model.client() as client:
        result = AgentRunner(
            client,
            MarketTools(queries(), treasury_queries(), perpetual, macro_queries()),
            settings(),
            now=lambda: NOW,
        ).run("Private question")
    assert result.limitations == [LimitationCode.DATABASE_UNAVAILABLE] and not result.evidence
    assert "secret" not in result.answer and "private SQL" not in result.answer


def test_instructions_preserve_native_units_times_and_unsupported_calculations() -> None:
    prompt = instructions(NOW)
    for text in (
        "ten supplied",
        "complete UTC hours",
        "exact millisecond",
        "longs pay shorts",
        "USDC collateral",
        "source_event_at is null",
        "local receipts",
        "No historical OI tool",
        "Do not annualize",
        "not historical",
        "manual refresh or collection",
    ):
        assert text in prompt
    assert END.isoformat() in prompt and (END - timedelta(hours=24)).isoformat() in prompt
