"""Fixed Coinbase BTC/USD and nominal Treasury reader queries only."""

import json
from typing import Any, Literal

from openai.types.responses import FunctionToolParam

from market_intelligence.agent.models import (
    LatestArguments,
    StrictArguments,
    ToolEvidence,
    TreasuryCurveArguments,
    TreasurySpreadArguments,
    WindowArguments,
)
from market_intelligence.ingestion.models import parse_instant
from market_intelligence.queries.models import UnknownMarketError, validate_window
from market_intelligence.queries.service import MarketQueries
from market_intelligence.treasury.queries import TreasuryQueries
from market_intelligence.treasury.query_models import treasury_date

LATEST: Literal["get_latest_btc_candle"] = "get_latest_btc_candle"
SUMMARY: Literal["get_btc_window_summary"] = "get_btc_window_summary"
TREASURY_CURVE: Literal["get_treasury_curve"] = "get_treasury_curve"
TREASURY_SPREADS: Literal["get_treasury_spread_history"] = "get_treasury_spread_history"
TOOL_NAMES = (LATEST, SUMMARY, TREASURY_CURVE, TREASURY_SPREADS)


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
        (
            TREASURY_CURVE,
            "Read all fourteen nominal Treasury tenors and the 10Y-minus-2Y spread for one "
            "source date; includes native percent yields, missing reasons and provenance",
            TreasuryCurveArguments,
        ),
        (
            TREASURY_SPREADS,
            "Read up to twenty stored Treasury source dates per page in [start, end), with "
            "2Y/10Y percent yields and signed spreads in percentage points and basis points. "
            "Coverage counts the whole window's stored dates/tenors, not calendar completeness. "
            "Follow next_cursor with unchanged bounds; pages are separate snapshots. "
            "No window aggregates or date alignment with BTC are calculated",
            TreasurySpreadArguments,
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
    def __init__(self, queries: MarketQueries, treasury_queries: TreasuryQueries) -> None:
        self.queries = queries
        self.treasury_queries = treasury_queries

    def execute(self, name: str, arguments: str, call_id: str) -> ToolEvidence:
        if name not in TOOL_NAMES:
            raise LookupError("Unknown tool")
        values = unique_json_object(arguments)
        if name == TREASURY_CURVE:
            curve = TreasuryCurveArguments.model_validate(values)
            return ToolEvidence(
                call_id=call_id,
                name=TREASURY_CURVE,
                arguments=curve.model_dump(),
                result=self.treasury_queries.curve(treasury_date(curve.observed_on)),
            )
        if name == TREASURY_SPREADS:
            history = TreasurySpreadArguments.model_validate(values)
            return ToolEvidence(
                call_id=call_id,
                name=TREASURY_SPREADS,
                arguments=history.model_dump(),
                result=self.treasury_queries.spread_page(
                    treasury_date(history.start), treasury_date(history.end), 20, history.cursor
                ),
            )
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
