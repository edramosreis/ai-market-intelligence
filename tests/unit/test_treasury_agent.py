"""Strict Treasury tools, bounded continuation, and exact SDK evidence relay."""

import json
from datetime import date
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from market_intelligence.agent.instructions import instructions
from market_intelligence.agent.models import LimitationCode, ToolEvidence
from market_intelligence.agent.runner import AgentRunner
from market_intelligence.agent.tools import (
    SUMMARY,
    TOOL_NAMES,
    TREASURY_CURVE,
    TREASURY_SPREADS,
    MarketTools,
    definitions,
)
from market_intelligence.treasury.queries import TreasuryQueries
from market_intelligence.treasury.query_models import (
    TreasuryCurveResult,
    TreasurySpreadPage,
    treasury_cursor,
)
from tests.agent_fakes import (
    FakeModel,
    function,
    hyperliquid_queries,
    message,
    queries,
    response,
    settings,
    treasury_curve,
    treasury_queries,
    treasury_spreads,
)
from tests.unit.test_treasury_queries import NOW

CURVE = '{"observed_on":"2024-01-01"}'
HISTORY = '{"start":"2024-01-01","end":"2024-01-02","cursor":null}'


def test_allowlist_has_seven_closed_tools_with_required_nullable_treasury_cursor() -> None:
    tools = definitions()
    assert {tool["name"] for tool in tools} == set(TOOL_NAMES)
    assert len(tools) == 7
    schema = cast(
        dict[str, Any],
        next(tool["parameters"] for tool in tools if tool["name"] == TREASURY_SPREADS),
    )
    assert schema is not None
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"start", "end", "cursor"}
    assert schema["properties"]["cursor"]["type"] == ["string", "null"]
    assert schema["properties"]["cursor"]["enum"] == [None]


def test_cursor_schema_keeps_only_latest_server_continuations_for_each_window() -> None:
    page = treasury_spreads()
    other = page.model_copy(update={"end": date(2024, 1, 4)})
    first = treasury_cursor(page.start, page.end, page.start)
    second = treasury_cursor(other.start, other.end, date(2024, 1, 2))
    pages = [
        page.model_copy(update={"next_cursor": first}),
        other.model_copy(update={"next_cursor": second}),
        page,
    ]
    evidence = [
        ToolEvidence(
            call_id=f"call_{index}",
            name=TREASURY_SPREADS,
            arguments={
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "cursor": None,
            },
            result=item,
        )
        for index, item in enumerate(pages)
    ]
    tools = definitions(evidence)
    schema = cast(
        dict[str, Any],
        next(tool["parameters"] for tool in tools if tool["name"] == TREASURY_SPREADS),
    )
    assert schema["properties"]["cursor"]["enum"] == [None, second]
    assert len(tools) == 7 and all(tool["strict"] for tool in tools)
    fresh = cast(
        dict[str, Any],
        next(tool["parameters"] for tool in definitions() if tool["name"] == TREASURY_SPREADS),
    )
    assert fresh["properties"]["cursor"]["enum"] == [None]


@pytest.mark.parametrize("name,arguments", [(TREASURY_CURVE, CURVE), (TREASURY_SPREADS, HISTORY)])
def test_treasury_tools_execute_only_the_fixed_dataset_and_relay_exact_evidence(
    name: str, arguments: str
) -> None:
    btc, treasury = queries(), treasury_queries()
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
    model = FakeModel(
        response(reasoning, function(name, arguments)),
        response(message("US Treasury same-date spread: -0.125 percentage points / -12.5 bps.")),
    )
    with model.client() as client:
        agent = AgentRunner(
            client, MarketTools(btc, treasury, hyperliquid_queries()), settings(), now=lambda: NOW
        )
        result = agent.run("What was the Treasury curve or spread on January 1, 2024?")
    assert result.status == "answered" and result.limitations == []
    assert not btc.mock_calls
    expected: TreasuryCurveResult | TreasurySpreadPage
    if name == TREASURY_CURVE:
        treasury.curve.assert_called_once_with(date(2024, 1, 1))
        expected = treasury_curve()
    else:
        treasury.spread_page.assert_called_once_with(date(2024, 1, 1), date(2024, 1, 2), 20, None)
        expected = treasury_spreads()
    assert result.evidence[0].result == expected
    payload = json.loads(model.requests[1]["input"][-1]["output"])
    assert payload == expected.model_dump(mode="json")
    assert payload["source_code"] == "us_treasury" and payload["yield_unit"] == "percent"
    assert model.requests[1]["input"][1]["encrypted_content"] == "opaque"


@pytest.mark.parametrize(
    "name,arguments",
    [
        (TREASURY_CURVE, '{"observed_on":"20240101"}'),
        (TREASURY_CURVE, '{"observed_on":"2024-01-01T00:00:00Z"}'),
        (TREASURY_CURVE, '{"observed_on":"2024-02-30"}'),
        (TREASURY_CURVE, '{"observed_on":20240101}'),
        (TREASURY_CURVE, '{"observed_on":"2024-01-01","source":"other"}'),
        (TREASURY_CURVE, '{"observed_on":"2024-01-01","observed_on":"2024-01-02"}'),
        (TREASURY_SPREADS, '{"start":"2024-01-01","end":"2024-01-02"}'),
        (TREASURY_SPREADS, '{"start":"2024-01-01","end":"2024-01-02","cursor":false}'),
        (TREASURY_SPREADS, '{"start":"2024-01-01","end":"2024-01-02","cursor":""}'),
        (
            TREASURY_SPREADS,
            '{"start":"2024-01-01","end":"2024-01-02","cursor":null,"limit":100}',
        ),
    ],
)
def test_invalid_treasury_argument_shapes_do_not_reach_queries(name: str, arguments: str) -> None:
    btc, treasury = queries(), treasury_queries()
    model = FakeModel(response(function(name, arguments)))
    with model.client() as client:
        result = AgentRunner(
            client, MarketTools(btc, treasury, hyperliquid_queries()), settings(), now=lambda: NOW
        ).run("Treasury rates?")
    assert result.limitations == [LimitationCode.INVALID_ARGUMENTS]
    assert not btc.mock_calls and not treasury.mock_calls and not result.evidence


@pytest.mark.parametrize(
    "name,arguments",
    [
        (TREASURY_CURVE, '{"observed_on":"1989-12-31"}'),
        (TREASURY_CURVE, '{"observed_on":"2027-01-01"}'),
        (TREASURY_SPREADS, '{"start":"2024-01-02","end":"2024-01-01","cursor":null}'),
        (TREASURY_SPREADS, '{"start":"2024-01-01","end":"2040-01-01","cursor":null}'),
        (TREASURY_SPREADS, '{"start":"1990-01-01","end":"2024-01-01","cursor":null}'),
        (TREASURY_SPREADS, '{"start":"2024-01-01","end":"2024-01-02","cursor":"!bad"}'),
        (
            TREASURY_SPREADS,
            json.dumps(
                {
                    "start": "2024-01-01",
                    "end": "2024-01-02",
                    "cursor": treasury_cursor(date(2024, 1, 1), date(2024, 2, 1), date(2024, 1, 2)),
                }
            ),
        ),
    ],
)
def test_invalid_dates_and_cursor_binding_fail_before_database_access(
    name: str, arguments: str
) -> None:
    engine = create_engine("postgresql+psycopg://unused:unused@127.0.0.1:1/unused")
    model = FakeModel(response(function(name, arguments)))
    try:
        with model.client() as client:
            result = AgentRunner(
                client,
                MarketTools(
                    queries(), TreasuryQueries(engine, now=lambda: NOW), hyperliquid_queries()
                ),
                settings(),
                now=lambda: NOW,
            ).run("Treasury rates?")
    finally:
        engine.dispose()
    assert result.limitations == [LimitationCode.INVALID_ARGUMENTS] and not result.evidence


def test_mixed_btc_and_treasury_tools_share_the_three_call_four_request_budget() -> None:
    btc, treasury = queries(), treasury_queries()
    model = FakeModel(
        response(function(SUMMARY, '{"start":"2024-01-01","end":"2024-01-01T00:15:00Z"}')),
        response(function(TREASURY_CURVE, CURVE, "call_2")),
        response(function(TREASURY_SPREADS, HISTORY, "call_3")),
        response(message("BTC and Treasury observations have separate source/window evidence.")),
    )
    with model.client() as client:
        result = AgentRunner(
            client, MarketTools(btc, treasury, hyperliquid_queries()), settings(), now=lambda: NOW
        ).run("Give the stored BTC summary and Treasury evidence for these dates.")
    assert result.status == "answered" and len(result.evidence) == 3
    assert result.model_requests == 4 and result.tool_calls == 3
    assert model.requests[-1]["tool_choice"] == "none"
    assert [item.name for item in result.evidence] == [SUMMARY, TREASURY_CURVE, TREASURY_SPREADS]


@pytest.mark.parametrize("continue_history", [False, True])
def test_partial_history_requires_a_complete_cursor_chain(continue_history: bool) -> None:
    treasury = treasury_queries()
    original = treasury_spreads().model_copy(update={"end": date(2024, 1, 3)})
    token = treasury_cursor(original.start, original.end, original.start)
    first = original.model_copy(update={"next_cursor": token})
    last = original.model_copy(
        update={
            "observations": [
                original.observations[0].model_copy(update={"observed_on": date(2024, 1, 2)})
            ]
        }
    )
    treasury.spread_page.side_effect = [first, last]
    replies = [
        response(
            function(TREASURY_SPREADS, '{"start":"2024-01-01","end":"2024-01-03","cursor":null}')
        )
    ]
    if continue_history:
        replies.append(
            response(
                function(
                    TREASURY_SPREADS,
                    json.dumps({"start": "2024-01-01", "end": "2024-01-03", "cursor": token}),
                    "call_2",
                )
            )
        )
    replies.append(response(message("Whole requested history is available.")))
    model = FakeModel(*replies)
    with model.client() as client:
        result = AgentRunner(
            client,
            MarketTools(queries(), treasury, hyperliquid_queries()),
            settings(),
            now=lambda: NOW,
        ).run("Give the Treasury spread history.")
    assert result.status == ("answered" if continue_history else "limited")
    assert result.limitations == ([] if continue_history else [LimitationCode.PARTIAL_RESULTS])
    assert isinstance(result.evidence[0].result, TreasurySpreadPage)
    assert result.evidence[0].result.next_cursor == token
    first_schema = cast(
        dict[str, Any],
        next(
            tool["parameters"]
            for tool in model.requests[0]["tools"]
            if tool["name"] == TREASURY_SPREADS
        ),
    )
    next_schema = cast(
        dict[str, Any],
        next(
            tool["parameters"]
            for tool in model.requests[1]["tools"]
            if tool["name"] == TREASURY_SPREADS
        ),
    )
    assert first_schema["properties"]["cursor"]["enum"] == [None]
    assert next_schema["properties"]["cursor"]["enum"] == [None, token]
    if continue_history:
        assert treasury.spread_page.call_args.args[-1] == token
        final_schema = cast(
            dict[str, Any],
            next(
                tool["parameters"]
                for tool in model.requests[-1]["tools"]
                if tool["name"] == TREASURY_SPREADS
            ),
        )
        assert final_schema["properties"]["cursor"]["enum"] == [None]
    else:
        assert "Whole requested history" not in result.answer


def test_starting_with_an_external_cursor_cannot_establish_whole_window_results() -> None:
    page = treasury_spreads()
    treasury = treasury_queries()
    treasury.spread_page.return_value = page.model_copy(update={"observations": []})
    args = json.dumps(
        {
            "start": "2024-01-01",
            "end": "2024-01-02",
            "cursor": treasury_cursor(page.start, page.end, page.start),
        }
    )
    model = FakeModel(response(function(TREASURY_SPREADS, args)), response(message()))
    with model.client() as client:
        result = AgentRunner(
            client,
            MarketTools(queries(), treasury, hyperliquid_queries()),
            settings(),
            now=lambda: NOW,
        ).run("Treasury history?")
    assert result.limitations == [LimitationCode.PARTIAL_RESULTS]


def test_treasury_database_errors_are_sanitized_without_model_retry() -> None:
    treasury = treasury_queries()
    treasury.curve.side_effect = OperationalError("private-sql", {}, Exception("secret"))
    model = FakeModel(response(function(TREASURY_CURVE, CURVE)))
    with model.client() as client:
        result = AgentRunner(
            client,
            MarketTools(queries(), treasury, hyperliquid_queries()),
            settings(),
            now=lambda: NOW,
        ).run("Private question")
    assert result.limitations == [LimitationCode.DATABASE_UNAVAILABLE]
    assert len(model.requests) == 1 and not result.evidence
    assert "private" not in result.model_dump_json() and "secret" not in result.model_dump_json()


def test_oversized_treasury_evidence_is_omitted() -> None:
    treasury = treasury_queries()
    page = treasury_spreads()
    treasury.spread_page.return_value = page.model_copy(
        update={"observations": page.observations * 100}
    )
    model = FakeModel(response(function(TREASURY_SPREADS, HISTORY)))
    with model.client() as client:
        result = AgentRunner(
            client,
            MarketTools(queries(), treasury, hyperliquid_queries()),
            settings(),
            now=lambda: NOW,
        ).run("Treasury history?")
    assert result.limitations == [LimitationCode.OUTPUT_LIMIT] and not result.evidence
    assert len(model.requests) == 1 and len(result.model_dump_json().encode()) < 32000


def test_instructions_preserve_treasury_units_missingness_and_cross_domain_boundaries() -> None:
    prompt = instructions(NOW)
    for text in (
        "seven supplied",
        "percentage points",
        "basis points",
        "source_null",
        "field_absent",
        "not_stored",
        "cursor null",
        "separate database snapshots",
        "not vintages",
        "forward-fill",
    ):
        assert text in prompt
