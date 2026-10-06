"""Bounded Responses loop; all tool evidence is collected by application code."""

import json
import threading
import time
from collections.abc import Callable
from datetime import datetime
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
from market_intelligence.agent.tools import LATEST, SUMMARY, MarketTools, definitions
from market_intelligence.config import AgentSettings
from market_intelligence.ingestion.models import as_utc, utc_now
from market_intelligence.queries.models import Latest, UnknownMarketError

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
        "The agent requested invalid tool arguments. Use an explicit UTC date or a "
        "five-minute-aligned window."
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
            return AgentResult(
                status="limited" if code else "answered",
                answer=MESSAGES[code] if code else (answer or ""),
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
                    tools=definitions(),
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
            if call.name not in (LATEST, SUMMARY):
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
                item = self.tools.execute(call.name, call.arguments, call.call_id)
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
            elif item.result.coverage.status == "no_data":
                return finish(LimitationCode.NO_DATA)
            elif item.result.coverage.status == "incomplete":
                return finish(LimitationCode.INCOMPLETE)
            # Relay complete output items, including reasoning, for stateless continuation.
            history.extend(
                cast(ResponseInputItemParam, output.model_dump(mode="json", exclude_none=True))
                for output in response.output
            )
            history.append(
                {"type": "function_call_output", "call_id": call.call_id, "output": payload}
            )
        return finish(LimitationCode.BUDGET_EXCEEDED)
