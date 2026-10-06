"""Validated questions, tool arguments, and application-collected evidence."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from market_intelligence.queries.models import Latest, Summary


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


class AgentQuestion(StrictArguments):
    question: str = Field(min_length=1, max_length=4000)


class LimitationCode(StrEnum):
    NO_DATA = "no_data"
    INCOMPLETE = "incomplete"
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
    name: Literal["get_latest_btc_candle", "get_btc_window_summary"]
    arguments: dict[str, str]
    result: Latest | Summary


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
