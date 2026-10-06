"""Synthetic evidence and the real OpenAI SDK with an in-memory HTTP transport."""

import json
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import Mock

import httpx
from openai import OpenAI

from market_intelligence.config import AgentSettings, ApiSettings
from market_intelligence.queries.models import (
    CandleEvidence,
    Coverage,
    Latest,
    Market,
    Provenance,
    Summary,
)
from market_intelligence.queries.service import MarketQueries

START = datetime(2024, 1, 1, tzinfo=UTC)
END = START + timedelta(minutes=15)
NOW = START + timedelta(minutes=20)


def settings(**overrides: Any) -> AgentSettings:
    return AgentSettings(
        **{
            "_env_file": None,
            "enabled": True,
            "OPENAI_API_KEY": "synthetic-unit-test-key",
            "OPENAI_MODEL": "test-model",
            **overrides,
        }
    )


def summary() -> Summary:
    return Summary(
        market=Market(
            id=1,
            source_code="coinbase_exchange",
            source_name="Coinbase Exchange",
            source_product_id="BTC-USD",
            base_asset_code="BTC",
            quote_asset_code="USD",
            earliest_opened_at=START,
            latest_opened_at=END - timedelta(minutes=5),
        ),
        start=START,
        end=END,
        retrieved_at=NOW,
        coverage=Coverage(
            status="complete",
            expected_buckets=3,
            actual_buckets=3,
            missing_buckets=0,
            ratio=Decimal("1.00000000"),
            observed_start=START,
            observed_end=END,
            missing_ranges=[],
            missing_range_count=0,
            missing_ranges_truncated=False,
        ),
        opening_price=Decimal(100),
        closing_price=Decimal(105),
        open_to_close_return_percent=Decimal("5.00000000"),
        high=Decimal(111),
        low=Decimal(94),
        base_volume=Decimal("0.6"),
        provenance=Provenance(first_ingested_at=NOW, last_updated_at=NOW, ingestion_run_count=1),
    )


def latest() -> Latest:
    window = summary()
    return Latest(
        market=window.market,
        status="available",
        retrieved_at=NOW,
        candle=CandleEvidence(
            opened_at=END - timedelta(minutes=5),
            ended_at=END,
            interval_seconds=300,
            status="complete",
            expected_constituents=1,
            actual_constituents=1,
            open=Decimal(104),
            high=Decimal(111),
            low=Decimal(95),
            close=Decimal(105),
            base_volume=Decimal("0.3"),
            provenance=window.provenance,
        ),
        age_seconds=Decimal(300),
        stale=False,
        stale_after_seconds=900,
    )


def queries() -> Mock:
    result = Mock(spec=MarketQueries)
    result.settings = ApiSettings(_env_file=None)  # type: ignore[call-arg]
    result.list_markets.return_value = [summary().market]
    result.summary.return_value = summary()
    result.latest.return_value = latest()
    return result


def function(name: str, arguments: str = "{}", call_id: str = "call_1") -> dict[str, Any]:
    return {
        "type": "function_call",
        "id": "fc_" + call_id,
        "name": name,
        "arguments": arguments,
        "call_id": call_id,
        "status": "completed",
    }


def message(
    text: str = "Coinbase BTC/USD rose 5% during the requested UTC window.",
) -> dict[str, Any]:
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def response(*output: dict[str, Any], status: str = "completed") -> dict[str, Any]:
    return {
        "id": "resp_synthetic",
        "object": "response",
        "created_at": NOW.timestamp(),
        "model": "test-model",
        "status": status,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "output": list(output),
    }


class FakeModel:
    def __init__(
        self,
        *replies: dict[str, Any] | httpx.Response | Exception,
        before_response: Callable[[int, dict[str, Any]], None] | None = None,
    ) -> None:
        self.replies = deque(replies)
        self.requests: list[dict[str, Any]] = []
        self.before_response = before_response

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and str(request.url) == "https://model.test/v1/responses"
        data = json.loads(request.content)
        self.requests.append(data)
        if self.before_response:
            self.before_response(len(self.requests), data)
        assert self.replies, "Unexpected extra model request"
        reply = self.replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    @contextmanager
    def client(self) -> Iterator[OpenAI]:
        with OpenAI(
            api_key="synthetic-unit-test-key",
            base_url="https://model.test/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(self.handle)),
        ) as client:
            yield client
