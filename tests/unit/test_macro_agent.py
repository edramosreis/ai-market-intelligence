"""Strict native macro tools, exact SDK evidence and bounded pagination."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from market_intelligence.agent.instructions import instructions
from market_intelligence.agent.models import LimitationCode, ToolEvidence
from market_intelligence.agent.runner import AgentRunner
from market_intelligence.agent.tools import (
    MACRO_HISTORY,
    MACRO_LATEST,
    MACRO_VERSIONS,
    SUMMARY,
    TOOL_NAMES,
    TREASURY_SPREADS,
    MarketTools,
    definitions,
)
from market_intelligence.config import ApiSettings
from market_intelligence.macro.models import BLS_NOTICE, CATALOG, MacroSeries
from market_intelligence.macro.queries import MacroQueries
from market_intelligence.macro.query_models import (
    MacroCoverage,
    MacroLatest,
    MacroObservationEvidence,
    MacroObservationPage,
    MacroProvenance,
    MacroReceiptEvidence,
    MacroSeriesEvidence,
    MacroVersionPage,
)
from tests.agent_fakes import (
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

NOW = datetime(2026, 10, 9, tzinfo=UTC)
START, END = date(2024, 1, 1), date(2024, 2, 1)
SERIES = MacroSeries.CPI.value
HISTORY = {"series_id": SERIES, "start": START.isoformat(), "end": END.isoformat(), "cursor": None}
VERSIONS = {"series_id": SERIES, "month": START.isoformat(), "cursor": None}


def results(
    series: MacroSeries = MacroSeries.CPI,
) -> tuple[MacroLatest, MacroObservationPage, MacroVersionPage]:
    native = CATALOG[series]
    metadata = MacroSeriesEvidence(
        series_id=series,
        source_code=cast(Any, native.provider),
        source_name="BLS" if native.provider == "bls" else "Federal Reserve Board",
        source_url="https://www.bls.gov"
        if native.provider == "bls"
        else "https://www.federalreserve.gov",
        title=native.title,
        unit=cast(Any, native.unit),
        seasonal_adjustment=cast(Any, native.seasonal_adjustment),
        earliest_native_month=native.earliest_month,
        stored_months=1,
        first_stored_month=START,
        last_stored_month=START,
        source_notice=BLS_NOTICE if native.provider == "bls" else None,
    )
    observation = MacroObservationEvidence(
        month=START,
        native_period="2024-01-31" if series == MacroSeries.FED_FUNDS else "2024-M01",
        version_number=1,
        status="available",
        value=Decimal("100.125") if series == MacroSeries.CPI else Decimal(0),
        missing_reason=None,
        footnotes=[],
        provenance=MacroProvenance(
            first_materialized_at=NOW, materialized_at=NOW, content_receipt_id=UUID(int=1)
        ),
    )
    receipt = MacroReceiptEvidence(
        run_id=UUID(int=1),
        start=START,
        end=END,
        fetch_started_at=NOW,
        received_at=NOW,
        access_date=NOW.date(),
        finished_at=NOW,
        received_periods=1,
        retained_periods=0,
        prepared_text=None,
        source_annotations=[],
        source_messages=[],
        latest_hints=[],
    )
    base: dict[str, Any] = {"series": metadata, "retrieved_at": NOW, "receipts": [receipt]}
    latest = MacroLatest(
        **base,
        status="stored",
        observation=observation,
        latest_completed_month=date(2026, 9, 1),
        months_behind_latest_completed=32,
    )
    history = MacroObservationPage(
        **base,
        start=START,
        end=END,
        observations=[observation],
        next_cursor=None,
        coverage=MacroCoverage(
            status="complete",
            expected_months=1,
            stored_months=1,
            available_values=1,
            source_missing_values=0,
            not_stored_months=0,
            first_stored_month=START,
            last_stored_month=START,
            missing_ranges=[],
            missing_range_count=0,
            missing_ranges_truncated=False,
        ),
    )
    versions = MacroVersionPage(
        **base,
        month=START,
        current_version_number=1,
        observed_versions=1,
        versions=[observation],
        next_cursor=None,
    )
    return latest, history, versions


def data() -> Mock:
    result = macro_queries()
    (
        result.latest.return_value,
        result.observations.return_value,
        result.observed_versions.return_value,
    ) = results()
    return result


def tools(macro: Mock) -> MarketTools:
    return MarketTools(queries(), treasury_queries(), hyperliquid_queries(), macro)


def run(model: FakeModel, macro: Mock, **overrides: Any) -> Any:
    with model.client() as client:
        return AgentRunner(client, tools(macro), settings(**overrides), now=lambda: NOW).run(
            "Stored macro evidence?"
        )


@pytest.mark.parametrize("series", list(MacroSeries))
@pytest.mark.parametrize("name", [MACRO_LATEST, MACRO_HISTORY, MACRO_VERSIONS])
def test_exact_native_evidence_and_reader_dispatch(series: MacroSeries, name: str) -> None:
    macro = data()
    expected = dict(
        zip((MACRO_LATEST, MACRO_HISTORY, MACRO_VERSIONS), results(series), strict=True)
    )[name]
    arguments = {
        **(HISTORY if name == MACRO_HISTORY else VERSIONS if name == MACRO_VERSIONS else {}),
        "series_id": series.value,
    }
    if name == MACRO_LATEST:
        macro.latest.return_value = expected
    elif name == MACRO_HISTORY:
        macro.observations.return_value = expected
    else:
        macro.observed_versions.return_value = expected
    model = FakeModel(
        response(function(name, json.dumps(arguments))),
        response(message("Native stored macro evidence.")),
    )
    result = run(model, macro)
    assert result.status == "answered" and result.limitations == []
    assert result.evidence[0].result == expected
    assert json.loads(model.requests[1]["input"][-1]["output"]) == expected.model_dump(mode="json")
    if name == MACRO_LATEST:
        macro.latest.assert_called_once_with(series.value)
    elif name == MACRO_HISTORY:
        macro.observations.assert_called_once_with(series.value, START, END, 20, None)
    else:
        macro.observed_versions.assert_called_once_with(series.value, START, 20, None)


def test_schemas_are_closed_fixed_series_and_required_nullable_cursors() -> None:
    schemas = definitions()
    assert len(schemas) == 10 and {item["name"] for item in schemas} == set(TOOL_NAMES)
    for item in schemas:
        assert item["strict"]
        schema = cast(dict[str, Any], item["parameters"])
        assert schema["additionalProperties"] is False
        assert set(schema.get("required", [])) == set(schema["properties"])
        if item["name"] in (MACRO_LATEST, MACRO_HISTORY, MACRO_VERSIONS):
            assert set(schema["properties"]["series_id"]["enum"]) == {s.value for s in MacroSeries}
        if item["name"] in (MACRO_HISTORY, MACRO_VERSIONS):
            assert schema["properties"]["cursor"]["enum"] == [None]


@pytest.mark.parametrize(
    "name,arguments",
    [
        (MACRO_LATEST, "{}"),
        (MACRO_LATEST, '{"series_id":"DFF"}'),
        (MACRO_LATEST, '{"series_id":true}'),
        (MACRO_LATEST, '{"series_id":"CUSR0000SA0","series_id":"LNS14000000"}'),
        (MACRO_LATEST, '{"series_id":"CUSR0000SA0","sql":"SELECT 1"}'),
        (MACRO_HISTORY, json.dumps({**HISTORY, "start": "2024-01-02"})),
        (MACRO_HISTORY, json.dumps({**HISTORY, "start": "2024-13-01"})),
        (MACRO_HISTORY, json.dumps({**HISTORY, "end": "2024-02-01T00:00:00Z"})),
        (MACRO_HISTORY, json.dumps({k: v for k, v in HISTORY.items() if k != "cursor"})),
        (MACRO_HISTORY, json.dumps({**HISTORY, "cursor": "invented"})),
        (MACRO_HISTORY, json.dumps({**HISTORY, "cursor": False})),
        (MACRO_HISTORY, json.dumps({**HISTORY, "limit": 100})),
        (MACRO_VERSIONS, json.dumps({**VERSIONS, "month": "2024-02-30"})),
        (MACRO_VERSIONS, json.dumps({**VERSIONS, "cursor": "invented"})),
    ],
)
def test_invalid_arguments_stop_before_any_macro_reader(name: str, arguments: str) -> None:
    macro = data()
    result = run(FakeModel(response(function(name, arguments))), macro)
    assert result.limitations == [LimitationCode.INVALID_ARGUMENTS]
    assert not macro.mock_calls


@pytest.mark.parametrize(
    "condition,code",
    [
        ("latest_empty", LimitationCode.NO_DATA),
        ("history_empty", LimitationCode.NO_DATA),
        ("versions_empty", LimitationCode.NO_DATA),
        ("history_gap", LimitationCode.INCOMPLETE),
        ("latest_dash", LimitationCode.MISSING_VALUES),
        ("history_dash", LimitationCode.MISSING_VALUES),
    ],
)
def test_missing_storage_and_source_dash_have_distinct_server_limitations(
    condition: str, code: LimitationCode
) -> None:
    macro = data()
    latest, history, versions = results()
    name: str
    arguments: dict[str, str | None]
    missing = (
        latest.observation.model_copy(
            update={"status": "source_missing", "value": None, "missing_reason": "source_dash"}
        )
        if latest.observation
        else None
    )
    if condition.startswith("latest"):
        name, arguments = MACRO_LATEST, {"series_id": SERIES}
        macro.latest.return_value = latest.model_copy(
            update={
                "observation": None if condition.endswith("empty") else missing,
                "status": "no_data" if condition.endswith("empty") else "stored",
            }
        )
    elif condition.startswith("history"):
        name, arguments = MACRO_HISTORY, HISTORY
        updates = {
            "status": "no_data"
            if condition.endswith("empty")
            else "incomplete"
            if condition.endswith("gap")
            else "complete",
            "source_missing_values": 1 if condition.endswith("dash") else 0,
        }
        macro.observations.return_value = history.model_copy(
            update={"coverage": history.coverage.model_copy(update=updates)}
        )
    else:
        name, arguments = MACRO_VERSIONS, VERSIONS
        macro.observed_versions.return_value = versions.model_copy(
            update={"observed_versions": 0, "versions": []}
        )
    model = FakeModel(response(function(name, json.dumps(arguments))))
    result = run(model, macro)
    assert result.limitations == [code] and len(result.evidence) == 1
    assert len(model.requests) == 1 and "Coinbase" not in result.answer


@pytest.mark.parametrize("name", [MACRO_HISTORY, MACRO_VERSIONS])
def test_completed_chain_retires_cursor_and_early_answer_is_partial(name: str) -> None:
    macro = data()
    arguments = HISTORY if name == MACRO_HISTORY else VERSIONS
    reader = macro.observations if name == MACRO_HISTORY else macro.observed_versions
    page = results()[1 if name == MACRO_HISTORY else 2]
    first = page.model_copy(update={"next_cursor": "issued"})
    reader.side_effect = [first, page]
    model = FakeModel(
        response(function(name, json.dumps(arguments), "first")),
        response(function(name, json.dumps({**arguments, "cursor": "issued"}), "second")),
        response(message("Complete stored history.")),
    )
    result = run(model, macro)
    assert result.status == "answered" and reader.call_count == 2
    for index, enum in [(1, [None, "issued"]), (2, [None])]:
        definition = next(t for t in model.requests[index]["tools"] if t["name"] == name)
        assert definition["parameters"]["properties"]["cursor"]["enum"] == enum
    reader.side_effect = None
    reader.return_value = first
    partial = run(
        FakeModel(
            response(function(name, json.dumps(arguments))),
            response(message("Everything retrieved.")),
        ),
        macro,
    )
    assert partial.limitations == [LimitationCode.PARTIAL_RESULTS]


@pytest.mark.parametrize("change", ["series_id", "end", "completed"])
def test_cursor_cannot_cross_queries_or_reuse_completed_chain(change: str) -> None:
    macro = data()
    page = results()[1].model_copy(
        update={"next_cursor": None if change == "completed" else "issued"}
    )
    macro.observations.return_value = page
    arguments = {**HISTORY, "cursor": "issued"}
    if change == "series_id":
        arguments[change] = MacroSeries.UNEMPLOYMENT.value
    elif change == "end":
        arguments[change] = "2024-03-01"
    model = FakeModel(
        response(function(MACRO_HISTORY, json.dumps(HISTORY), "first")),
        response(function(MACRO_HISTORY, json.dumps(arguments), "second")),
    )
    result = run(model, macro)
    assert result.limitations == [LimitationCode.INVALID_ARGUMENTS]
    assert macro.observations.call_count == 1


def test_pending_macro_chain_survives_another_domain_answer() -> None:
    macro = data()
    macro.observations.return_value = results()[1].model_copy(update={"next_cursor": "issued"})
    model = FakeModel(
        response(function(MACRO_HISTORY, json.dumps(HISTORY), "first")),
        response(function(SUMMARY, '{"start":"2024-01-01","end":"2024-01-02"}', "second")),
        response(message("All domains complete.")),
    )
    result = run(model, macro)
    assert result.limitations == [LimitationCode.PARTIAL_RESULTS]
    assert "macro" in result.answer and "Treasury" not in result.answer


def test_database_failure_and_oversized_receipts_are_controlled() -> None:
    macro = data()
    macro.latest.side_effect = OperationalError("secret SQL", {}, Exception("private"))
    result = run(
        FakeModel(response(function(MACRO_LATEST, json.dumps({"series_id": SERIES})))), macro
    )
    assert result.limitations == [LimitationCode.DATABASE_UNAVAILABLE]
    assert "secret" not in result.answer and result.evidence == []
    macro.latest.side_effect = None
    result = run(
        FakeModel(response(function(MACRO_LATEST, json.dumps({"series_id": SERIES})))),
        macro,
        max_tool_output_bytes=1024,
    )
    assert result.limitations == [LimitationCode.OUTPUT_LIMIT] and result.evidence == []


def test_instructions_preserve_month_units_receipts_and_vintage_boundaries() -> None:
    text = instructions(NOW)
    for required in [
        "ten supplied",
        "CUSR0000SA0",
        "LNS14000000",
        "RIFSPFF_N.M",
        "2026-10-01",
        "not an inflation percentage",
        "Month lag",
        "source-dash",
        "retrospective historical release vintages",
        "No macro tool fetches BLS/Fed",
        "content-origin receipts",
    ]:
        assert required in text


@pytest.mark.parametrize(
    "arguments",
    [
        {**HISTORY, "start": "1946-01-01"},
        {**HISTORY, "start": "2024-02-01", "end": "2024-01-01"},
        {**HISTORY, "end": "2026-11-01"},
        {**HISTORY, "end": "2024-04-01"},
    ],
)
def test_actual_reader_bounds_fail_before_database_access(arguments: dict[str, str | None]) -> None:
    actual = MacroQueries(
        cast(Engine, object()),
        ApiSettings(_env_file=None, macro_max_window_months=2),  # type: ignore[call-arg]
        now=lambda: NOW,
    )
    model = FakeModel(response(function(MACRO_HISTORY, json.dumps(arguments))))
    with model.client() as client:
        result = AgentRunner(
            client,
            MarketTools(queries(), treasury_queries(), hyperliquid_queries(), actual),
            settings(),
            now=lambda: NOW,
        ).run("Requested macro months?")
    assert result.limitations == [LimitationCode.INVALID_ARGUMENTS]


def test_macro_cursor_state_is_not_shared_between_questions() -> None:
    macro = data()
    macro.observations.return_value = results()[1].model_copy(update={"next_cursor": "issued"})
    model = FakeModel(
        response(function(MACRO_HISTORY, json.dumps(HISTORY), "first")),
        response(message("Partial history.")),
        response(function(MACRO_HISTORY, json.dumps({**HISTORY, "cursor": "issued"}), "second")),
    )
    with model.client() as client:
        runner = AgentRunner(client, tools(macro), settings(), now=lambda: NOW)
        assert runner.run("First history?").limitations == [LimitationCode.PARTIAL_RESULTS]
        assert runner.run("Separate history?").limitations == [LimitationCode.INVALID_ARGUMENTS]
    assert macro.observations.call_count == 1
    definition = next(t for t in model.requests[2]["tools"] if t["name"] == MACRO_HISTORY)
    assert definition["parameters"]["properties"]["cursor"]["enum"] == [None]


def test_macro_cursor_choices_are_separate_by_kind_and_latest_per_window() -> None:
    history, versions = results()[1:]
    other = history.model_copy(update={"end": date(2024, 3, 1)})
    evidence = [
        ToolEvidence(
            call_id=str(index),
            name=cast(Any, name),
            arguments=cast(Any, arguments),
            result=cast(MacroObservationPage | MacroVersionPage, page),
        )
        for index, (name, arguments, page) in enumerate(
            [
                (MACRO_HISTORY, HISTORY, history.model_copy(update={"next_cursor": "retired"})),
                (
                    MACRO_HISTORY,
                    {**HISTORY, "end": other.end.isoformat()},
                    other.model_copy(update={"next_cursor": "other-window"}),
                ),
                (
                    MACRO_VERSIONS,
                    VERSIONS,
                    versions.model_copy(update={"next_cursor": "version-only"}),
                ),
                (MACRO_HISTORY, {**HISTORY, "cursor": "retired"}, history),
            ]
        )
    ]
    schema = {t["name"]: cast(dict[str, Any], t["parameters"]) for t in definitions(evidence)}
    assert schema[MACRO_HISTORY]["properties"]["cursor"]["enum"] == [None, "other-window"]
    assert schema[MACRO_VERSIONS]["properties"]["cursor"]["enum"] == [None, "version-only"]


@pytest.mark.parametrize("macro_pending", [False, True])
def test_partial_treasury_and_macro_histories_keep_correct_domain_messages(
    macro_pending: bool,
) -> None:
    macro = data()
    macro.observations.return_value = results()[1].model_copy(
        update={"next_cursor": "macro-pending" if macro_pending else None}
    )
    readers = tools(macro)
    treasury = cast(Mock, readers.treasury_queries)
    treasury.spread_page.return_value = treasury.spread_page.return_value.model_copy(
        update={"next_cursor": "treasury-pending"}
    )
    model = FakeModel(
        response(
            function(
                TREASURY_SPREADS, '{"start":"2024-01-01","end":"2024-01-02","cursor":null}', "first"
            )
        ),
        response(function(MACRO_HISTORY, json.dumps(HISTORY), "second")),
        response(message("Everything complete.")),
    )
    with model.client() as client:
        result = AgentRunner(client, readers, settings(), now=lambda: NOW).run(
            "Treasury and macro histories?"
        )
    assert result.limitations == [LimitationCode.PARTIAL_RESULTS]
    assert "Treasury" in result.answer
    assert ("macro" in result.answer) == macro_pending
