"""Synthetic SDK replies execute native macro readers through the actual HTTP handler."""

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.pool import QueuePool

from market_intelligence.agent.tools import MACRO_HISTORY, MACRO_LATEST, MACRO_VERSIONS
from market_intelligence.api import create_app
from market_intelligence.db.macro_store import MacroStore
from market_intelligence.macro.models import Footnote, MacroProvider, MacroSeries, MonthlyWindow
from market_intelligence.macro.queries import MacroQueries
from market_intelligence.macro.query_models import (
    MacroObservationPage,
    MacroVersionPage,
    month_at,
    month_number,
)
from tests.agent_fakes import FakeModel, function, message, response, settings
from tests.integration.test_hyperliquid_agent import counts as prior_counts
from tests.integration.test_macro_queries import END, NOW, START, counts, load, observation
from tests.integration.test_queries import reader as reader

pytestmark = pytest.mark.integration
RETRIEVED = NOW + timedelta(hours=1)
SERIES = MacroSeries.CPI.value
HISTORY = {"series_id": SERIES, "start": START.isoformat(), "end": END.isoformat(), "cursor": None}
VERSIONS = {"series_id": SERIES, "month": START.isoformat(), "cursor": None}


def pool_check(reader: Engine) -> None:
    assert cast(QueuePool, reader.pool).checkedout() == 0


@pytest.mark.parametrize("series", list(MacroSeries))
@pytest.mark.parametrize("name", [MACRO_LATEST, MACRO_HISTORY, MACRO_VERSIONS])
def test_exact_reader_http_sdk_evidence_and_no_writes_or_held_connections(
    reader: Engine, admin_engine: Engine, macro_store: MacroStore, series: MacroSeries, name: str
) -> None:
    load(
        macro_store,
        tuple(
            observation(
                series,
                month_at(month_number(START) + index),
                "100.125" if series == MacroSeries.CPI else "0",
            )
            for index in range(4)
        ),
        provider=MacroProvider.FED if series == MacroSeries.FED_FUNDS else MacroProvider.BLS,
    )
    before = (prior_counts(admin_engine), counts(admin_engine))
    arguments = {
        **(HISTORY if name == MACRO_HISTORY else VERSIONS if name == MACRO_VERSIONS else {}),
        "series_id": series.value,
    }
    model = FakeModel(
        response(function(name, json.dumps(arguments))),
        response(message("Native stored macro observations and limitations.")),
        before_response=lambda index, request: pool_check(reader),
    )
    with (
        model.client() as client,
        TestClient(
            create_app(
                engine=reader, agent_settings=settings(), model_client=client, now=lambda: RETRIEVED
            )
        ) as http,
    ):
        reply = http.post("/v1/agent/query", json={"question": "Stored macro evidence?"})
        route = (
            "latest"
            if name == MACRO_LATEST
            else "observations"
            if name == MACRO_HISTORY
            else "versions"
        )
        params = {
            key: value for key, value in arguments.items() if key not in ("series_id", "cursor")
        }
        params["limit"] = "20"
        deterministic = http.get(f"/v1/macro/series/{series.value}/{route}", params=params).json()
    payload = reply.json()
    assert reply.status_code == 200 and payload["status"] == "answered"
    evidence = payload["evidence"][0]["result"]
    assert evidence == deterministic
    assert json.loads(model.requests[1]["input"][-1]["output"]) == evidence
    assert evidence["historical_release_vintages"] == "not_established"
    assert evidence["receipts"][0]["per_observation_publication_time"] == "not_established"
    if name == MACRO_LATEST:
        assert evidence["months_behind_latest_completed"] == 29
        assert evidence["observation"]["month"] == "2024-04-01"
    if series == MacroSeries.FED_FUNDS:
        assert evidence["series"]["unit"] == "percent_per_annum"
        row = (
            evidence.get("observation") or evidence.get("observations", evidence.get("versions"))[0]
        )
        assert row["native_period"].endswith(("01-31", "04-30"))
        assert isinstance(row["value"], str) and Decimal(row["value"]) == 0
        assert evidence["receipts"][0]["prepared_text"] == "2026-10-09T08:30:00"
    else:
        assert evidence["series"]["seasonal_adjustment"] == "seasonally_adjusted"
        assert "BLS.gov" in evidence["series"]["source_notice"]
    assert (prior_counts(admin_engine), counts(admin_engine)) == before
    pool_check(reader)


@pytest.mark.parametrize(
    "condition,limitation",
    [
        ("latest_empty", "no_data"),
        ("history_empty", "no_data"),
        ("versions_empty", "no_data"),
        ("history_gap", "incomplete"),
        ("latest_dash", "missing_values"),
        ("history_dash", "missing_values"),
    ],
)
def test_absent_storage_and_source_missing_values_stop_with_exact_native_evidence(
    reader: Engine, admin_engine: Engine, macro_store: MacroStore, condition: str, limitation: str
) -> None:
    if not condition.endswith("empty"):
        months = 1 if condition == "history_gap" else 4
        load(
            macro_store,
            tuple(
                observation(
                    month=month_at(month_number(START) + index),
                    value=None if condition.endswith("dash") else "100.125",
                    footnotes=(Footnote("U", "Synthetic source unavailable"),)
                    if condition.endswith("dash")
                    else (),
                )
                for index in range(months)
            ),
        )
    before = counts(admin_engine)
    name = (
        MACRO_LATEST
        if condition.startswith("latest")
        else MACRO_HISTORY
        if condition.startswith("history")
        else MACRO_VERSIONS
    )
    arguments = (
        HISTORY
        if name == MACRO_HISTORY
        else VERSIONS
        if name == MACRO_VERSIONS
        else {"series_id": SERIES}
    )
    model = FakeModel(
        response(function(name, json.dumps(arguments))),
        before_response=lambda index, request: pool_check(reader),
    )
    with (
        model.client() as client,
        TestClient(
            create_app(
                engine=reader, agent_settings=settings(), model_client=client, now=lambda: RETRIEVED
            )
        ) as http,
    ):
        reply = http.post("/v1/agent/query", json={"question": "Stored macro values?"})
    payload = reply.json()
    assert reply.status_code == 200 and payload["limitations"] == [limitation]
    assert len(model.requests) == 1 and len(payload["evidence"]) == 1
    evidence = payload["evidence"][0]["result"]
    if condition == "history_gap":
        assert evidence["coverage"]["not_stored_months"] == 3
        assert evidence["coverage"]["source_missing_values"] == 0
    elif condition.endswith("dash"):
        row = evidence.get("observation") or evidence["observations"][0]
        assert row["value"] is None and row["missing_reason"] == "source_dash"
        assert row["footnotes"] == [{"code": "U", "text": "Synthetic source unavailable"}]
    assert "Coinbase" not in payload["answer"] and counts(admin_engine) == before


@pytest.mark.parametrize("name", [MACRO_HISTORY, MACRO_VERSIONS])
@pytest.mark.parametrize("complete", [True, False])
def test_real_twenty_row_pagination_and_abandoned_chains(
    reader: Engine, admin_engine: Engine, macro_store: MacroStore, name: str, complete: bool
) -> None:
    first: MacroObservationPage | MacroVersionPage
    if name == MACRO_HISTORY:
        end = month_at(month_number(START) + 25)
        load(
            macro_store,
            tuple(observation(month=month_at(month_number(START) + index)) for index in range(25)),
            window=MonthlyWindow(START, end),
        )
        arguments = {**HISTORY, "end": end.isoformat()}
        first = MacroQueries(reader, now=lambda: RETRIEVED).observations(SERIES, START, end, 20)
    else:
        for index in range(25):
            load(macro_store, (observation(value=str(100 + index)),), seconds=index * 4)
        arguments = VERSIONS
        first = MacroQueries(reader, now=lambda: RETRIEVED).observed_versions(SERIES, START, 20)
    assert first.next_cursor is not None
    replies = [response(function(name, json.dumps(arguments), "first"))]
    if complete:
        replies.append(
            response(
                function(name, json.dumps({**arguments, "cursor": first.next_cursor}), "second")
            )
        )
    replies.append(response(message("All requested rows retrieved.")))
    before = counts(admin_engine)
    model = FakeModel(*replies, before_response=lambda index, request: pool_check(reader))
    with (
        model.client() as client,
        TestClient(
            create_app(
                engine=reader, agent_settings=settings(), model_client=client, now=lambda: RETRIEVED
            )
        ) as http,
    ):
        payload = http.post("/v1/agent/query", json={"question": "Stored macro history?"}).json()
    assert payload["status"] == ("answered" if complete else "limited")
    assert payload["limitations"] == ([] if complete else ["partial_results"])
    rows = "observations" if name == MACRO_HISTORY else "versions"
    assert len(payload["evidence"][0]["result"][rows]) == 20
    definition = next(t for t in model.requests[1]["tools"] if t["name"] == name)
    assert definition["parameters"]["properties"]["cursor"]["enum"] == [None, first.next_cursor]
    if complete:
        assert len(payload["evidence"][1]["result"][rows]) == 5
        assert payload["evidence"][1]["result"]["next_cursor"] is None
        definition = next(t for t in model.requests[2]["tools"] if t["name"] == name)
        assert definition["parameters"]["properties"]["cursor"]["enum"] == [None]
    assert counts(admin_engine) == before


@pytest.mark.parametrize("replay", ["correction", "unchanged", "omission"])
def test_collected_evidence_keeps_content_origin_after_writes_during_model_wait(
    reader: Engine, macro_store: MacroStore, replay: str
) -> None:
    initial_notes = (Footnote("A", "Original footnote"),)
    load(
        macro_store,
        (observation(footnotes=initial_notes),),
        window=MonthlyWindow(START, date(2024, 2, 1)),
    )
    original = MacroQueries(reader, now=lambda: RETRIEVED).observed_versions(SERIES, START, 20)

    def update(index: int, request: dict[str, Any]) -> None:
        pool_check(reader)
        if index == 2:
            rows = (
                ()
                if replay == "omission"
                else (
                    observation(
                        value="101.5" if replay == "correction" else "100.125",
                        footnotes=(Footnote("B", "Corrected note"),)
                        if replay == "correction"
                        else initial_notes,
                    ),
                )
            )
            load(macro_store, rows, seconds=10, window=MonthlyWindow(START, date(2024, 2, 1)))

    model = FakeModel(
        response(function(MACRO_VERSIONS, json.dumps(VERSIONS))),
        response(message("Changes observed locally.")),
        before_response=update,
    )
    with (
        model.client() as client,
        TestClient(
            create_app(
                engine=reader, agent_settings=settings(), model_client=client, now=lambda: RETRIEVED
            )
        ) as http,
    ):
        payload = http.post(
            "/v1/agent/query", json={"question": "Locally observed changes for January 2024 CPI?"}
        ).json()
        after = http.get(
            f"/v1/macro/series/{SERIES}/versions", params={"month": START.isoformat(), "limit": 20}
        ).json()
    collected = payload["evidence"][0]["result"]
    assert payload["status"] == "answered" and collected == original.model_dump(mode="json")
    assert json.loads(model.requests[1]["input"][-1]["output"]) == collected
    assert after["observed_versions"] == (2 if replay == "correction" else 1)
    assert after["versions"][0] == collected["versions"][0]
    assert after["receipts"][0] == collected["receipts"][0]
    if replay == "correction":
        assert Decimal(after["versions"][1]["value"]) == Decimal("101.5")
        assert after["versions"][1]["footnotes"] == [{"code": "B", "text": "Corrected note"}]


def test_missing_to_available_local_versions_preserve_ordered_notes_and_both_receipts(
    reader: Engine, macro_store: MacroStore
) -> None:
    load(
        macro_store,
        (
            observation(
                MacroSeries.UNEMPLOYMENT, value=None, footnotes=(Footnote("U", "Unavailable"),)
            ),
        ),
    )
    load(
        macro_store,
        (
            observation(
                MacroSeries.UNEMPLOYMENT,
                value="0",
                footnotes=(Footnote("A", "First"), Footnote("B", "Second")),
            ),
        ),
        seconds=10,
    )
    model = FakeModel(
        response(
            function(
                MACRO_VERSIONS,
                json.dumps({**VERSIONS, "series_id": MacroSeries.UNEMPLOYMENT.value}),
            )
        ),
        response(message("Locally observed correction from unavailable to zero.")),
    )
    with (
        model.client() as client,
        TestClient(
            create_app(
                engine=reader, agent_settings=settings(), model_client=client, now=lambda: RETRIEVED
            )
        ) as http,
    ):
        payload = http.post(
            "/v1/agent/query", json={"question": "Observed January unemployment versions?"}
        ).json()
    assert payload["status"] == "answered"
    result = payload["evidence"][0]["result"]
    assert result["current_version_number"] == 2 and len(result["receipts"]) == 2
    assert result["versions"][0]["missing_reason"] == "source_dash"
    assert Decimal(result["versions"][1]["value"]) == 0
    assert result["versions"][1]["footnotes"] == [
        {"code": "A", "text": "First"},
        {"code": "B", "text": "Second"},
    ]


@pytest.mark.parametrize("extra_call", [False, True])
def test_all_three_series_share_three_tool_four_model_request_budget(
    reader: Engine, macro_store: MacroStore, extra_call: bool
) -> None:
    for series in MacroSeries:
        load(
            macro_store,
            (observation(series, value="100.125" if series == MacroSeries.CPI else "0"),),
            provider=MacroProvider.FED if series == MacroSeries.FED_FUNDS else MacroProvider.BLS,
        )
    replies = [
        response(function(MACRO_LATEST, json.dumps({"series_id": series.value}), f"call_{index}"))
        for index, series in enumerate(MacroSeries)
    ]
    replies.append(
        response(function(MACRO_LATEST, json.dumps({"series_id": SERIES}), "fourth"))
        if extra_call
        else response(message("Stored BLS and FRB values with native months and units."))
    )
    model = FakeModel(*replies, before_response=lambda index, request: pool_check(reader))
    with (
        model.client() as client,
        TestClient(
            create_app(
                engine=reader, agent_settings=settings(), model_client=client, now=lambda: RETRIEVED
            )
        ) as http,
    ):
        payload = http.post(
            "/v1/agent/query",
            json={"question": "Latest stored CPI, unemployment and effective Fed funds?"},
        ).json()
    assert payload["model_requests"] == 4 and payload["tool_calls"] == 3
    assert len(payload["evidence"]) == 3
    assert payload["limitations"] == (["budget_exceeded"] if extra_call else [])
    assert model.requests[-1]["tool_choice"] == "none"
