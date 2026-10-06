"""Only application-selected Coinbase BTC/USD queries can be executed."""

import json
from typing import Any, Literal

from openai.types.responses import FunctionToolParam

from market_intelligence.agent.models import (
    LatestArguments,
    StrictArguments,
    ToolEvidence,
    WindowArguments,
)
from market_intelligence.ingestion.models import parse_instant
from market_intelligence.queries.models import UnknownMarketError, validate_window
from market_intelligence.queries.service import MarketQueries

LATEST: Literal["get_latest_btc_candle"] = "get_latest_btc_candle"
SUMMARY: Literal["get_btc_window_summary"] = "get_btc_window_summary"


def definitions() -> list[FunctionToolParam]:
    specifications: list[tuple[str, str, type[StrictArguments]]] = [
        (
            LATEST,
            "Read the latest stored completed Coinbase BTC/USD candle, with UTC age and staleness",
            LatestArguments,
        ),
        (
            SUMMARY,
            "Read a Coinbase BTC/USD window summary with calculated metrics, coverage, and gaps",
            WindowArguments,
        ),
    ]
    return [
        {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": arguments.model_json_schema(),
            "strict": True,
        }
        for name, description, arguments in specifications
    ]


def unique_json_object(value: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON argument")
            result[key] = item
        return result

    decoded = json.loads(value, object_pairs_hook=pairs)
    if not isinstance(decoded, dict):
        raise ValueError("Tool arguments must be an object")
    return decoded


class MarketTools:
    def __init__(self, queries: MarketQueries) -> None:
        self.queries = queries

    def execute(self, name: str, arguments: str, call_id: str) -> ToolEvidence:
        if name not in (LATEST, SUMMARY):
            raise LookupError("Unknown tool")
        values = unique_json_object(arguments)
        validated: StrictArguments
        if name == LATEST:
            validated = LatestArguments.model_validate(values)
            window = None
        else:
            requested = WindowArguments.model_validate(values)
            validated = requested
            checked = validate_window(
                parse_instant(requested.start),
                parse_instant(requested.end),
                300,
                self.queries.settings.max_window_days,
            )
            window = (checked.start, checked.end)
        supported = [
            market.id
            for market in self.queries.list_markets()
            if market.source_code == "coinbase_exchange"
            and market.source_product_id == "BTC-USD"
            and market.base_asset_code == "BTC"
            and market.quote_asset_code == "USD"
            and market.canonical_interval_seconds == 300
        ]
        if len(supported) != 1:
            raise UnknownMarketError("Supported market unavailable")
        result = (
            self.queries.latest(supported[0])
            if window is None
            else self.queries.summary(supported[0], *window)
        )
        if (
            result.market.id != supported[0]
            or result.market.source_code != "coinbase_exchange"
            or result.market.source_product_id != "BTC-USD"
            or result.market.base_asset_code != "BTC"
            or result.market.quote_asset_code != "USD"
        ):
            raise UnknownMarketError("Supported market changed")
        return ToolEvidence(
            call_id=call_id,
            name=LATEST if name == LATEST else SUMMARY,
            arguments=validated.model_dump(),
            result=result,
        )
