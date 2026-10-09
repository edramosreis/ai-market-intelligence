"""Bounded Responses loop; all tool evidence is collected by application code."""

import json
import threading
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import cast

import httpx
from openai import APIError, OpenAI
from openai.types.responses import Response, ResponseInputItemParam
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.agent.instructions import instructions
from market_intelligence.agent.models import (
    AgentQuestion,
    AgentResult,
    AgentUnavailableError,
    LimitationCode,
    ToolEvidence,
)
from market_intelligence.agent.tools import TOOL_NAMES, MarketTools, definitions
from market_intelligence.config import AgentSettings
from market_intelligence.hyperliquid.query_models import (
    FundingLatest,
    FundingSummary,
    OpenInterestLatest,
)
from market_intelligence.ingestion.models import as_utc, utc_now
from market_intelligence.macro.query_models import (
    MacroLatest,
    MacroObservationPage,
    MacroVersionPage,
)
from market_intelligence.queries.models import Latest, Summary, UnknownMarketError
from market_intelligence.treasury.query_models import TreasuryCurveResult, TreasurySpreadPage

MESSAGES = {
    LimitationCode.NO_DATA: "No stored Coinbase BTC/USD observations are available for this query.",
    LimitationCode.INCOMPLETE: (
        "The requested Coinbase BTC/USD window has missing candles; full-window metrics "
        "are unavailable. Inspect the returned coverage evidence."
    ),
    LimitationCode.STALE: (
        "The latest stored Coinbase BTC/USD candle is stale. It cannot establish a current "
        "price; a manual ingestion refresh is needed."
    ),
    LimitationCode.INVALID_ARGUMENTS: (
        "The agent requested invalid tool arguments. Use YYYY-MM-DD source dates for "
        "Treasury, or explicit UTC dates/five-minute-aligned timestamps for BTC windows."
        " Hyperliquid funding windows require complete UTC hours from 2024."
        " Macro requests require one fixed series and completed YYYY-MM-01 months, "
        "with the latest server-issued cursor for the same query."
    ),
    LimitationCode.MISSING_RATES: (
        "Stored Treasury evidence has unavailable yields or benchmark inputs. Missing rates "
        "are not zero, and unavailable spreads cannot be calculated. Inspect the returned "
        "missing reasons and any continuation cursor."
    ),
    LimitationCode.PARTIAL_RESULTS: (
        "Only part of the requested Treasury spread history was retrieved. The returned "
        "evidence does not establish a whole-window result; use the continuation cursor "
        "or a narrower date window. Pages are separate snapshots."
    ),
    LimitationCode.UNKNOWN_TOOL: "The agent requested an unsupported tool; none was executed.",
    LimitationCode.DATABASE_UNAVAILABLE: "Stored market observations are temporarily unavailable.",
    LimitationCode.MODEL_UNAVAILABLE: (
        "The model service is temporarily unavailable; any collected evidence is retained below."
    ),
    LimitationCode.INVALID_MODEL_RESPONSE: "The model returned an unsupported or invalid response.",
    LimitationCode.UNGROUNDED_ANSWER: (
        "No answer was accepted because the model did not execute a data tool."
    ),
    LimitationCode.BUDGET_EXCEEDED: (
        "The agent reached its request/tool-call budget before completing an answer."
    ),
    LimitationCode.DEADLINE_EXCEEDED: (
        "The agent reached its execution deadline; any completed evidence is retained below."
    ),
    LimitationCode.OUTPUT_LIMIT: "The model or evidence exceeded the configured output limit.",
    LimitationCode.INPUT_LIMIT: "The question exceeds the configured input limit or is empty.",
}

TREASURY_MESSAGES = {
    LimitationCode.NO_DATA: (
        "No stored Treasury source dates are available for this query. No holiday or "
        "publication-calendar completeness can be inferred from that absence."
    ),
    LimitationCode.INCOMPLETE: (
        "Stored Treasury curves have missing normalized tenor rows. Inspect the returned "
        "not-stored reasons and available benchmark evidence."
    ),
}


FUNDING_MESSAGES = {
    LimitationCode.NO_DATA: (
        "No stored settled Hyperliquid BTC perpetual funding events are available for this "
        "query. Missing funding is unavailable, not zero."
    ),
    LimitationCode.INCOMPLETE: (
        "The requested Hyperliquid BTC perpetual funding window has missing UTC settlement "
        "hours; full-window sums and means are unavailable. Inspect the returned coverage."
    ),
    LimitationCode.STALE: (
        "The latest stored settled Hyperliquid BTC perpetual funding event is stale. "
        "It cannot establish current funding; a manual funding refresh is needed."
    ),
}

OI_MESSAGES = {
    LimitationCode.NO_DATA: (
        "No stored Hyperliquid BTC perpetual open-interest receipts are available. "
        "Historical OI cannot be inferred; manual collection is needed."
    ),
    LimitationCode.STALE: (
        "The latest stored Hyperliquid BTC perpetual open-interest receipt is stale. "
        "It cannot establish current OI; manual collection is needed. Its timestamp is "
        "a local receipt time, not an exchange event time."
    ),
}


MACRO_MESSAGES = {
    LimitationCode.NO_DATA: (
        "No stored macro observations or locally observed versions are available for this "
        "query. Uncollected months do not establish source omissions; no provider fetch "
        "was initiated. Inspect the selected series and requested months."
    ),
    LimitationCode.INCOMPLETE: (
        "The requested macro window has months absent from local storage. Returned values "
        "describe only stored months; missing months are unavailable, not zero. Inspect "
        "whole-window coverage and any continuation cursor. No automatic backfill occurs."
    ),
    LimitationCode.MISSING_VALUES: (
        "Stored macro evidence includes an explicit source-missing value. It is unavailable, "
        "not zero; inspect the native month, missing reason, source footnotes and receipts. "
        "Do not substitute an earlier available value."
    ),
    LimitationCode.PARTIAL_RESULTS: (
        "Only part of the requested macro history or locally observed versions was retrieved. "
        "Use the continuation cursor or a narrower request. Pages are separate snapshots; "
        "local versions do not establish historical publisher vintages."
    ),
}


def partial_macro_history(evidence: list[ToolEvidence]) -> bool:
    queries: dict[tuple[object, ...], tuple[bool, str | None]] = {}
    key: tuple[object, ...]
    for item in evidence:
        page = item.result
        if isinstance(page, MacroObservationPage):
            key = (item.name, page.series.series_id, page.start, page.end)
        elif isinstance(page, MacroVersionPage):
            key = (item.name, page.series.series_id, page.month)
        else:
            continue
        cursor = item.arguments["cursor"]
        if cursor is None:
            queries[key] = (True, page.next_cursor)
        else:
            started, expected = queries.get(key, (False, None))
            queries[key] = (started and cursor == expected, page.next_cursor)
    return any(not started or cursor is not None for started, cursor in queries.values())


def partial_spread_history(evidence: list[ToolEvidence]) -> bool:
    windows: dict[tuple[date, date], tuple[bool, str | None]] = {}
    for item in evidence:
        if not isinstance(item.result, TreasurySpreadPage):
            continue
        page = item.result
        key = (page.start, page.end)
        cursor = item.arguments["cursor"]
        if cursor is None:
            windows[key] = (True, page.next_cursor)
        else:
            started, expected = windows.get(key, (False, None))
            windows[key] = (started and cursor == expected, page.next_cursor)
    return any(not started or cursor is not None for started, cursor in windows.values())


class AgentRunner:
    def __init__(
        self,
        client: OpenAI | None,
        tools: MarketTools,
        settings: AgentSettings,
        *,
        now: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client, self.tools, self.settings = client, tools, settings
        self.now, self.monotonic = now, monotonic
        self._slot = threading.Lock()

    def run(self, question: str) -> AgentResult:
        if self.client is None or not self.settings.configured:
            raise AgentUnavailableError("Agent disabled or missing OPENAI_API_KEY/OPENAI_MODEL")
        if not self._slot.acquire(blocking=False):
            raise AgentUnavailableError("Agent is busy; retry after the active request")
        try:
            return self._run(question)
        finally:
            self._slot.release()

    def _run(self, question: str) -> AgentResult:
        retrieved = as_utc(self.now())
        deadline = self.monotonic() + self.settings.deadline_seconds
        evidence: list[ToolEvidence] = []
        model_requests = tool_calls = 0

        def finish(code: LimitationCode | None, answer: str | None = None) -> AgentResult:
            partial_macro = code is None and partial_macro_history(evidence)
            partial_treasury = code is None and partial_spread_history(evidence)
            if partial_macro or partial_treasury:
                code = LimitationCode.PARTIAL_RESULTS
            explanation = MESSAGES.get(code) if code else None
            if (
                code
                and evidence
                and isinstance(evidence[-1].result, (TreasuryCurveResult, TreasurySpreadPage))
            ):
                explanation = TREASURY_MESSAGES.get(code, explanation)
            elif (
                code
                and evidence
                and isinstance(evidence[-1].result, (FundingLatest, FundingSummary))
            ):
                explanation = FUNDING_MESSAGES.get(code, explanation)
            elif code and evidence and isinstance(evidence[-1].result, OpenInterestLatest):
                explanation = OI_MESSAGES.get(code, explanation)
            if code and (
                partial_macro
                or (
                    evidence
                    and isinstance(
                        evidence[-1].result, (MacroLatest, MacroObservationPage, MacroVersionPage)
                    )
                )
            ):
                explanation = MACRO_MESSAGES.get(code, explanation)
            if partial_treasury:
                explanation = MESSAGES[LimitationCode.PARTIAL_RESULTS]
                if partial_macro:
                    explanation = (
                        "Requested Treasury and macro histories are only partly retrieved. "
                        "Inspect each continuation cursor or narrow the requests; pages "
                        "are separate snapshots and do not establish whole-window results."
                    )
            return AgentResult(
                status="limited" if code else "answered",
                answer=explanation if explanation else (answer or ""),
                evidence=evidence,
                limitations=[code] if code else [],
                model=self.settings.model,
                retrieved_at=retrieved,
                model_requests=model_requests,
                tool_calls=tool_calls,
            )

        try:
            AgentQuestion(question=question)
        except ValidationError:
            return finish(LimitationCode.INPUT_LIMIT)
        if not question.strip() or len(question) > self.settings.max_question_chars:
            return finish(LimitationCode.INPUT_LIMIT)
        prompt = instructions(retrieved)
        history: list[ResponseInputItemParam] = [{"role": "user", "content": question}]
        seen_call_ids: set[str] = set()
        assert self.client is not None and self.settings.model is not None
        for _ in range(self.settings.max_model_requests):
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return finish(LimitationCode.DEADLINE_EXCEEDED)
            if len((prompt + json.dumps(history)).encode()) > self.settings.max_context_bytes:
                return finish(LimitationCode.OUTPUT_LIMIT)
            model_requests += 1
            try:
                response = self.client.with_options(
                    max_retries=0,
                    timeout=httpx.Timeout(
                        remaining, connect=min(5, remaining), pool=min(5, remaining)
                    ),
                ).responses.create(
                    model=self.settings.model,
                    instructions=prompt,
                    input=history,
                    tools=definitions(evidence),
                    tool_choice="required"
                    if not evidence
                    else "none"
                    if tool_calls >= self.settings.max_tool_calls
                    else "auto",
                    parallel_tool_calls=False,
                    max_output_tokens=self.settings.max_output_tokens,
                    store=False,
                    include=["reasoning.encrypted_content"],
                )
            except APIError:
                return finish(LimitationCode.MODEL_UNAVAILABLE)
            if self.monotonic() >= deadline:
                return finish(LimitationCode.DEADLINE_EXCEEDED)
            try:
                # The SDK normally constructs models without full validation.
                response = Response.model_validate(response.model_dump())
            except ValidationError:
                return finish(LimitationCode.INVALID_MODEL_RESPONSE)
            if response.status == "incomplete":
                return finish(LimitationCode.OUTPUT_LIMIT)
            if response.status != "completed":
                return finish(LimitationCode.INVALID_MODEL_RESPONSE)
            if len(response.model_dump_json().encode()) > self.settings.max_response_bytes:
                return finish(LimitationCode.OUTPUT_LIMIT)
            if any(
                item.type not in ("function_call", "message", "reasoning")
                for item in response.output
            ):
                return finish(LimitationCode.INVALID_MODEL_RESPONSE)
            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                if not evidence:
                    return finish(LimitationCode.UNGROUNDED_ANSWER)
                answer = response.output_text.strip()
                if not answer:
                    return finish(LimitationCode.INVALID_MODEL_RESPONSE)
                if len(answer) > self.settings.max_answer_chars:
                    return finish(LimitationCode.OUTPUT_LIMIT)
                return finish(None, answer)
            if len(calls) != 1:
                return finish(LimitationCode.INVALID_MODEL_RESPONSE)
            call = calls[0]
            if call.name not in TOOL_NAMES:
                return finish(LimitationCode.UNKNOWN_TOOL)
            if (
                not call.call_id
                or len(call.call_id) > 200
                or call.call_id in seen_call_ids
                or len(call.arguments) > 1024
                or call.status not in (None, "completed")
            ):
                return finish(LimitationCode.INVALID_MODEL_RESPONSE)
            if tool_calls >= self.settings.max_tool_calls:
                return finish(LimitationCode.BUDGET_EXCEEDED)
            seen_call_ids.add(call.call_id)
            tool_calls += 1
            try:
                item = self.tools.execute(
                    call.name, call.arguments, call.call_id, evidence=evidence
                )
            except ValueError, ValidationError:
                return finish(LimitationCode.INVALID_ARGUMENTS)
            except SQLAlchemyError, UnknownMarketError:
                return finish(LimitationCode.DATABASE_UNAVAILABLE)
            evidence.append(item)
            payload = item.result.model_dump_json()
            if len(payload.encode()) > self.settings.max_tool_output_bytes:
                # Keep a bounded HTTP response as well as a bounded model context.
                evidence.pop()
                return finish(LimitationCode.OUTPUT_LIMIT)
            if self.monotonic() >= deadline:
                return finish(LimitationCode.DEADLINE_EXCEEDED)
            if isinstance(item.result, Latest):
                if item.result.status == "no_data":
                    return finish(LimitationCode.NO_DATA)
                if item.result.stale:
                    return finish(LimitationCode.STALE)
            elif isinstance(item.result, Summary):
                if item.result.coverage.status == "no_data":
                    return finish(LimitationCode.NO_DATA)
                if item.result.coverage.status == "incomplete":
                    return finish(LimitationCode.INCOMPLETE)
            elif isinstance(item.result, TreasuryCurveResult):
                if item.result.curve.status == "no_data":
                    return finish(LimitationCode.NO_DATA)
                if item.result.curve.status == "incomplete_stored_curve":
                    return finish(LimitationCode.INCOMPLETE)
                if item.result.curve.available_rates == 0:
                    return finish(LimitationCode.MISSING_RATES)
            elif isinstance(item.result, TreasurySpreadPage):
                if item.result.coverage.status == "no_data":
                    return finish(LimitationCode.NO_DATA)
                if any(
                    day.curve_status == "incomplete_stored_curve"
                    for day in item.result.observations
                ):
                    return finish(LimitationCode.INCOMPLETE)
                if any(day.spread.status == "unavailable" for day in item.result.observations):
                    return finish(LimitationCode.MISSING_RATES)
            elif isinstance(item.result, FundingSummary):
                if item.result.coverage.status == "no_data":
                    return finish(LimitationCode.NO_DATA)
                if item.result.coverage.status == "incomplete":
                    return finish(LimitationCode.INCOMPLETE)
            elif isinstance(item.result, (FundingLatest, OpenInterestLatest)):
                if item.result.status == "no_data":
                    return finish(LimitationCode.NO_DATA)
                if item.result.status == "stale":
                    return finish(LimitationCode.STALE)
            elif isinstance(item.result, MacroLatest):
                if item.result.observation is None:
                    return finish(LimitationCode.NO_DATA)
                if item.result.observation.status == "source_missing":
                    return finish(LimitationCode.MISSING_VALUES)
            elif isinstance(item.result, MacroObservationPage):
                if item.result.coverage.status == "no_data":
                    return finish(LimitationCode.NO_DATA)
                if item.result.coverage.status == "incomplete":
                    return finish(LimitationCode.INCOMPLETE)
                if item.result.coverage.source_missing_values:
                    return finish(LimitationCode.MISSING_VALUES)
            elif isinstance(item.result, MacroVersionPage):
                if item.result.observed_versions == 0:
                    return finish(LimitationCode.NO_DATA)
            # Relay complete output items, including reasoning, for stateless continuation.
            history.extend(
                cast(ResponseInputItemParam, output.model_dump(mode="json", exclude_none=True))
                for output in response.output
            )
            history.append(
                {"type": "function_call_output", "call_id": call.call_id, "output": payload}
            )
        return finish(LimitationCode.BUDGET_EXCEEDED)
