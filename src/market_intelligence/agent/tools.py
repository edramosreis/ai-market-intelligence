"""Fixed Coinbase, Treasury, Hyperliquid and native macro reader queries only."""

import json
from collections.abc import Sequence
from datetime import date
from typing import Any, Literal, cast

from openai.types.responses import FunctionToolParam

from market_intelligence.agent.models import (
    FundingWindowArguments,
    LatestArguments,
    MacroHistoryArguments,
    MacroSeriesArguments,
    MacroVersionsArguments,
    StrictArguments,
    ToolEvidence,
    TreasuryCurveArguments,
    TreasurySpreadArguments,
    WindowArguments,
)
from market_intelligence.hyperliquid.queries import HyperliquidQueries
from market_intelligence.ingestion.models import parse_instant
from market_intelligence.macro.queries import MacroQueries
from market_intelligence.macro.query_models import (
    MacroObservationPage,
    MacroVersionPage,
    month_date,
)
from market_intelligence.queries.models import UnknownMarketError, validate_window
from market_intelligence.queries.service import MarketQueries
from market_intelligence.treasury.queries import TreasuryQueries
from market_intelligence.treasury.query_models import TreasurySpreadPage, treasury_date

LATEST: Literal["get_latest_btc_candle"] = "get_latest_btc_candle"
SUMMARY: Literal["get_btc_window_summary"] = "get_btc_window_summary"
TREASURY_CURVE: Literal["get_treasury_curve"] = "get_treasury_curve"
TREASURY_SPREADS: Literal["get_treasury_spread_history"] = "get_treasury_spread_history"
FUNDING_LATEST: Literal["get_latest_btc_funding"] = "get_latest_btc_funding"
FUNDING_SUMMARY: Literal["get_btc_funding_summary"] = "get_btc_funding_summary"
OI_LATEST: Literal["get_latest_btc_open_interest"] = "get_latest_btc_open_interest"
MACRO_LATEST: Literal["get_latest_macro_observation"] = "get_latest_macro_observation"
MACRO_HISTORY: Literal["get_macro_observation_history"] = "get_macro_observation_history"
MACRO_VERSIONS: Literal["get_macro_observed_versions"] = "get_macro_observed_versions"
TOOL_NAMES = (
    LATEST,
    SUMMARY,
    TREASURY_CURVE,
    TREASURY_SPREADS,
    FUNDING_LATEST,
    FUNDING_SUMMARY,
    OI_LATEST,
    MACRO_LATEST,
    MACRO_HISTORY,
    MACRO_VERSIONS,
)


def definitions(evidence: Sequence[ToolEvidence] = ()) -> list[FunctionToolParam]:
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
        (
            FUNDING_LATEST,
            "Read the latest stored settled Hyperliquid BTC perpetual funding event, with "
            "exact source time, derived UTC settlement hour, signed hourly fraction, "
            "premium and staleness; positive funding means longs pay shorts",
            LatestArguments,
        ),
        (
            FUNDING_SUMMARY,
            "Read settled Hyperliquid BTC perpetual funding in an explicit [start, end) "
            "window of complete UTC hours from 2024. Returns coverage and gaps, arithmetic "
            "rate sum as fraction and percent, and mean hourly fraction. Metrics are "
            "unavailable with missing hours. No compounding, annualization or trader PnL",
            FundingWindowArguments,
        ),
        (
            OI_LATEST,
            "Read the latest stored Hyperliquid BTC perpetual OI receipt: quantity in BTC, "
            "mark/oracle prices in USDT, local fetch/receipt times, identity and staleness. "
            "No exchange event timestamp or historical completeness; no automatic refresh",
            LatestArguments,
        ),
        (
            MACRO_LATEST,
            "Read the latest stored completed month for one fixed BLS/FRB native series. "
            "Returns native units, adjustment, month lag and content-origin receipt; "
            "not publisher-current data, an inflation rate, or a verified publication delay",
            MacroSeriesArguments,
        ),
        (
            MACRO_HISTORY,
            "Read up to twenty current stored monthly observations in [start, end), with "
            "whole-window native month coverage, source-dash/absent distinctions, footnotes "
            "and content receipts. Follow next_cursor with the same series and bounds; "
            "pages are separate snapshots. No inflation, forward fill or alignment",
            MacroHistoryArguments,
        ),
        (
            MACRO_VERSIONS,
            "Read up to twenty immutable content versions observed locally for one series "
            "and month, with ordered footnotes and original collection receipts. Follow "
            "next_cursor with the same series/month. Not historical release vintages, "
            "publication timestamps or knowledge as of a past date",
            MacroVersionsArguments,
        ),
    ]
    tools: list[FunctionToolParam] = [
        {
            "type": "function",
            "name": name,
            "description": description,
            "parameters": arguments.model_json_schema(),
            "strict": True,
        }
        for name, description, arguments in specifications
    ]
    # Strict decoding can select server-issued tokens without transcribing opaque text.
    pending: dict[tuple[date, date], str | None] = {}
    for item in evidence:
        if isinstance(item.result, TreasurySpreadPage):
            page = item.result
            pending[(page.start, page.end)] = page.next_cursor
    cursors: list[str | None] = [None]
    cursors.extend(dict.fromkeys(token for token in pending.values() if token is not None))
    for tool in tools:
        if tool["name"] == TREASURY_SPREADS:
            parameters = tool["parameters"]
            assert parameters is not None
            properties = cast(dict[str, Any], parameters["properties"])
            original = properties["cursor"]
            properties["cursor"] = {
                "type": ["string", "null"],
                "enum": cursors,
                "description": original["description"],
            }
        elif tool["name"] in (MACRO_HISTORY, MACRO_VERSIONS):
            pending_macro: dict[tuple[object, ...], str | None] = {}
            for item in evidence:
                macro_page = item.result
                if tool["name"] == MACRO_HISTORY and isinstance(macro_page, MacroObservationPage):
                    pending_macro[
                        (macro_page.series.series_id, macro_page.start, macro_page.end)
                    ] = macro_page.next_cursor
                elif tool["name"] == MACRO_VERSIONS and isinstance(macro_page, MacroVersionPage):
                    pending_macro[(macro_page.series.series_id, macro_page.month)] = (
                        macro_page.next_cursor
                    )
            parameters = tool["parameters"]
            assert parameters is not None
            properties = cast(dict[str, Any], parameters["properties"])
            properties["cursor"] = {
                "type": ["string", "null"],
                "enum": [None, *dict.fromkeys(t for t in pending_macro.values() if t is not None)],
                "description": properties["cursor"]["description"],
            }
    return tools


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
    def __init__(
        self,
        queries: MarketQueries,
        treasury_queries: TreasuryQueries,
        hyperliquid_queries: HyperliquidQueries,
        macro_queries: MacroQueries,
    ) -> None:
        self.queries = queries
        self.treasury_queries = treasury_queries
        self.hyperliquid_queries = hyperliquid_queries
        self.macro_queries = macro_queries

    @staticmethod
    def validate_macro_cursor(
        name: str, arguments: dict[str, str | None], evidence: Sequence[ToolEvidence]
    ) -> None:
        cursor = arguments["cursor"]
        if cursor is None:
            return
        for item in reversed(evidence):
            if item.name != name:
                continue
            keys = (
                ("series_id", "start", "end") if name == MACRO_HISTORY else ("series_id", "month")
            )
            if any(item.arguments[key] != arguments[key] for key in keys):
                continue
            page = item.result
            if (
                isinstance(page, (MacroObservationPage, MacroVersionPage))
                and page.next_cursor == cursor
            ):
                return
            break
        raise ValueError("Use the latest server-issued cursor for the same macro query")

    def execute(
        self, name: str, arguments: str, call_id: str, *, evidence: Sequence[ToolEvidence] = ()
    ) -> ToolEvidence:
        if name not in TOOL_NAMES:
            raise LookupError("Unknown tool")
        values = unique_json_object(arguments)
        if name == MACRO_LATEST:
            macro = MacroSeriesArguments.model_validate(values)
            return ToolEvidence(
                call_id=call_id,
                name=MACRO_LATEST,
                arguments=macro.model_dump(),
                result=self.macro_queries.latest(macro.series_id),
            )
        if name == MACRO_HISTORY:
            history_macro = MacroHistoryArguments.model_validate(values)
            self.validate_macro_cursor(name, history_macro.model_dump(), evidence)
            return ToolEvidence(
                call_id=call_id,
                name=MACRO_HISTORY,
                arguments=history_macro.model_dump(),
                result=self.macro_queries.observations(
                    history_macro.series_id,
                    month_date(history_macro.start),
                    month_date(history_macro.end),
                    20,
                    history_macro.cursor,
                ),
            )
        if name == MACRO_VERSIONS:
            versions = MacroVersionsArguments.model_validate(values)
            self.validate_macro_cursor(name, versions.model_dump(), evidence)
            return ToolEvidence(
                call_id=call_id,
                name=MACRO_VERSIONS,
                arguments=versions.model_dump(),
                result=self.macro_queries.observed_versions(
                    versions.series_id,
                    month_date(versions.month),
                    20,
                    versions.cursor,
                ),
            )
        if name == FUNDING_SUMMARY:
            funding = FundingWindowArguments.model_validate(values)
            return ToolEvidence(
                call_id=call_id,
                name=FUNDING_SUMMARY,
                arguments=funding.model_dump(),
                result=self.hyperliquid_queries.funding_summary(
                    parse_instant(funding.start), parse_instant(funding.end)
                ),
            )
        if name in (FUNDING_LATEST, OI_LATEST):
            empty = LatestArguments.model_validate(values)
            return ToolEvidence(
                call_id=call_id,
                name=FUNDING_LATEST if name == FUNDING_LATEST else OI_LATEST,
                arguments=empty.model_dump(),
                result=self.hyperliquid_queries.latest_funding()
                if name == FUNDING_LATEST
                else self.hyperliquid_queries.latest_open_interest(),
            )
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
