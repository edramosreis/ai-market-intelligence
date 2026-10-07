"""Validated questions, tool arguments, and application-collected evidence."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from market_intelligence.hyperliquid.query_models import (
    FundingLatest,
    FundingSummary,
    OpenInterestLatest,
)
from market_intelligence.queries.models import Latest, Summary
from market_intelligence.treasury.query_models import TreasuryCurveResult, TreasurySpreadPage

ToolName = Literal[
    "get_latest_btc_candle",
    "get_btc_window_summary",
    "get_treasury_curve",
    "get_treasury_spread_history",
    "get_latest_btc_funding",
    "get_btc_funding_summary",
    "get_latest_btc_open_interest",
]


class StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class LatestArguments(StrictArguments):
    pass


class WindowArguments(StrictArguments):
    start: str = Field(
        min_length=10,
        max_length=40,
        description=(
            "Inclusive five-minute-aligned ISO date or timestamp with UTC offset; "
            "dates mean UTC midnight"
        ),
    )
    end: str = Field(
        min_length=10,
        max_length=40,
        description=(
            "Exclusive five-minute-aligned ISO date or timestamp with UTC offset, after start"
        ),
    )


class TreasuryCurveArguments(StrictArguments):
    observed_on: str = Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
        description="Exact Treasury source date as YYYY-MM-DD, from 1990 through today",
    )


class FundingWindowArguments(StrictArguments):
    start: str = Field(
        min_length=10,
        max_length=40,
        description=(
            "Inclusive UTC-hour-aligned ISO date or timestamp with UTC offset, from 2024; "
            "dates mean UTC midnight"
        ),
    )
    end: str = Field(
        min_length=10,
        max_length=40,
        description=(
            "Exclusive UTC-hour-aligned ISO date or timestamp with UTC offset, after start "
            "and no later than the current UTC hour boundary"
        ),
    )


class TreasurySpreadArguments(StrictArguments):
    start: str = Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
        description="Inclusive Treasury source date as YYYY-MM-DD, from 1990",
    )
    end: str = Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$",
        description="Exclusive Treasury source date as YYYY-MM-DD, after start",
    )
    cursor: str | None = Field(
        min_length=1,
        max_length=1024,
        description=(
            "Null for the first page; otherwise copy the returned next_cursor verbatim. "
            "Opaque token: do not decode or edit it; keep the same start/end window."
        ),
    )


class AgentQuestion(StrictArguments):
    question: str = Field(min_length=1, max_length=4000)


class LimitationCode(StrEnum):
    NO_DATA = "no_data"
    INCOMPLETE = "incomplete"
    MISSING_RATES = "missing_rates"
    PARTIAL_RESULTS = "partial_results"
    STALE = "stale"
    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    DATABASE_UNAVAILABLE = "database_unavailable"
    MODEL_UNAVAILABLE = "model_unavailable"
    INVALID_MODEL_RESPONSE = "invalid_model_response"
    UNGROUNDED_ANSWER = "ungrounded_answer"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    OUTPUT_LIMIT = "output_limit"
    INPUT_LIMIT = "input_limit"


class ToolEvidence(StrictArguments):
    call_id: str
    name: ToolName
    arguments: dict[str, str | None]
    result: (
        Latest
        | Summary
        | TreasuryCurveResult
        | TreasurySpreadPage
        | FundingLatest
        | FundingSummary
        | OpenInterestLatest
    )


class AgentResult(StrictArguments):
    status: Literal["answered", "limited"]
    answer: str
    evidence: list[ToolEvidence]
    limitations: list[LimitationCode]
    model: str | None
    retrieved_at: datetime
    model_requests: int
    tool_calls: int


class AgentUnavailableError(Exception):
    """Configuration is absent or the single in-flight request slot is occupied."""
