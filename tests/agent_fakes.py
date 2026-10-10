"""Synthetic evidence and the real OpenAI SDK with an in-memory HTTP transport."""

import json
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import Mock
from uuid import UUID

import httpx2 as httpx
from openai import OpenAI

from market_intelligence.config import AgentSettings, ApiSettings
from market_intelligence.hyperliquid.queries import HyperliquidQueries
from market_intelligence.macro.queries import MacroQueries
from market_intelligence.queries.models import (
    CandleEvidence,
    Coverage,
    Latest,
    Market,
    Provenance,
    Summary,
)
from market_intelligence.queries.service import MarketQueries
from market_intelligence.treasury.models import TreasuryMissingReason, TreasuryTenor
from market_intelligence.treasury.queries import TreasuryQueries
from market_intelligence.treasury.query_models import (
    TreasuryCoverage,
    TreasuryCurveEvidence,
    TreasuryCurveResult,
    TreasuryMonthRead,
    TreasuryProvenance,
    TreasuryRateEvidence,
    TreasurySpread,
    TreasurySpreadObservation,
    TreasurySpreadPage,
)

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


def hyperliquid_queries() -> Mock:
    return Mock(spec=HyperliquidQueries)


def macro_queries() -> Mock:
    return Mock(spec=MacroQueries)


def treasury_curve() -> TreasuryCurveResult:
    provenance = TreasuryProvenance(
        first_ingested_at=NOW,
        last_updated_at=NOW,
        last_ingestion_run_id=UUID(int=1),
    )
    values = {TreasuryTenor.TWO_YEARS: Decimal("4.25"), TreasuryTenor.TEN_YEARS: Decimal("4.125")}
    return TreasuryCurveResult(
        retrieved_at=NOW,
        curve=TreasuryCurveEvidence(
            observed_on=START.date(),
            status="stored",
            stored_rates=14,
            available_rates=2,
            rates=[
                TreasuryRateEvidence(
                    tenor=tenor,
                    yield_percent=values.get(tenor),
                    missing_reason=None if tenor in values else TreasuryMissingReason.FIELD_ABSENT,
                    provenance=provenance,
                )
                for tenor in TreasuryTenor
            ],
            spread=TreasurySpread(
                status="available",
                percentage_points=Decimal("-0.125"),
                basis_points=Decimal("-12.5"),
                missing_inputs=[],
            ),
            latest_month_read=TreasuryMonthRead(
                run_id=UUID(int=1),
                start=START.date(),
                end=(START + timedelta(days=31)).date(),
                finished_at=NOW,
                received_dates=1,
                retained_dates=0,
            ),
        ),
    )


def treasury_spreads() -> TreasurySpreadPage:
    curve = treasury_curve().curve
    return TreasurySpreadPage(
        retrieved_at=NOW,
        start=START.date(),
        end=(START + timedelta(days=1)).date(),
        coverage=TreasuryCoverage(
            status="observations_stored",
            observed_dates=1,
            stored_rates=14,
            available_rates=2,
            source_null=0,
            field_absent=12,
            first_observed_on=curve.observed_on,
            last_observed_on=curve.observed_on,
        ),
        observations=[
            TreasurySpreadObservation(
                observed_on=curve.observed_on,
                curve_status=curve.status,
                two_year=next(rate for rate in curve.rates if rate.tenor == "2Y"),
                ten_year=next(rate for rate in curve.rates if rate.tenor == "10Y"),
                spread=curve.spread,
                latest_month_read=curve.latest_month_read,
            )
        ],
        next_cursor=None,
    )


def treasury_queries() -> Mock:
    result = Mock(spec=TreasuryQueries)
    result.curve.return_value = treasury_curve()
    result.spread_page.return_value = treasury_spreads()
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
