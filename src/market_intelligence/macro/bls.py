"""Keyless BLS v1 native CPI/unemployment reads, bounded to ten inclusive years."""

import calendar
import json
import re
from datetime import date, timedelta
from typing import Any

from market_intelligence.macro._http import MacroTransport
from market_intelligence.macro.models import (
    Footnote,
    MacroError,
    MacroErrorCode,
    MacroSeries,
    MonthlyObservation,
    MonthlyWindow,
    ProviderRead,
    decimal_text,
)

BLS_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
MAX_RESPONSE_BYTES = 2_000_000
BLS_SERIES = (MacroSeries.CPI, MacroSeries.UNEMPLOYMENT)


def request_years(window: MonthlyWindow) -> tuple[int, int]:
    first, last = window.start.year, (window.end - timedelta(days=1)).year
    if first < 1947 or last - first >= 10:
        raise ValueError("BLS requests require at most ten inclusive years from 1947")
    return first, last


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def parse_bls(
    payload: bytes,
    window: MonthlyWindow,
) -> tuple[
    tuple[MonthlyObservation, ...], int, tuple[str, ...], tuple[tuple[MacroSeries, date], ...]
]:
    first, last = request_years(window)
    try:
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("Oversized JSON")
        body = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique)
        if not isinstance(body, dict) or set(body) != {
            "status",
            "responseTime",
            "message",
            "Results",
        }:
            raise ValueError("Unexpected response envelope")
        if body["status"] in {"REQUEST_FAILED", "REQUEST_NOT_PROCESSED"}:
            raise MacroError(MacroErrorCode.SOURCE_REJECTED)
        messages = body["message"]
        if (
            body["status"] != "REQUEST_SUCCEEDED"
            or type(body["responseTime"]) is not int
            or body["responseTime"] < 0
            or not isinstance(messages, list)
            or len(messages) > 50
            or any(not isinstance(msg, str) or len(msg) > 4000 for msg in messages)
            or not isinstance(body["Results"], dict)
            or set(body["Results"]) != {"series"}
        ):
            raise ValueError("Invalid response status or results")
        series_rows = body["Results"]["series"]
        if not isinstance(series_rows, list) or len(series_rows) != 2:
            raise ValueError("Missing requested series")
        seen_series: set[MacroSeries] = set()
        result: list[MonthlyObservation] = []
        annual_count = 0
        hints: list[tuple[MacroSeries, date]] = []
        for item in series_rows:
            if not isinstance(item, dict) or set(item) != {"seriesID", "data"}:
                raise ValueError("Unexpected series fields")
            series = MacroSeries(item["seriesID"])
            rows = item["data"]
            if (
                series not in BLS_SERIES
                or series in seen_series
                or not isinstance(rows, list)
                or len(rows) > 13 * (last - first + 1)
            ):
                raise ValueError("Ambiguous series or oversized history")
            seen_series.add(series)
            seen: set[tuple[int, int]] = set()
            hinted = False
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or set(row) - {"year", "period", "periodName", "value", "footnotes", "latest"}
                    or not {"year", "period", "periodName", "value", "footnotes"} <= set(row)
                    or not isinstance(row["year"], str)
                    or not re.fullmatch(r"[0-9]{4}", row["year"])
                    or not isinstance(row["period"], str)
                    or not re.fullmatch(r"M(?:0[1-9]|1[0-3])", row["period"])
                ):
                    raise ValueError("Invalid monthly fields")
                year, month = int(row["year"]), int(row["period"][1:])
                if not first <= year <= last or (year, month) in seen:
                    raise ValueError("Duplicate or out-of-request period")
                seen.add((year, month))
                if row["periodName"] != ("Annual" if month == 13 else calendar.month_name[month]):
                    raise ValueError("Period name disagrees with identity")
                notes = row["footnotes"]
                if not isinstance(notes, list) or len(notes) > 20:
                    raise ValueError("Invalid footnotes")
                footnotes: list[Footnote] = []
                for note in notes:
                    if note == {}:
                        continue
                    if not isinstance(note, dict) or set(note) != {"code", "text"}:
                        raise ValueError("Invalid footnote fields")
                    footnotes.append(Footnote(note["code"], note["text"]))
                value = None if row["value"] == "-" else decimal_text(row["value"])
                # Validate annual value/notes with the native series rules before excluding it.
                period = date(year, min(month, 12), 1)
                observation = MonthlyObservation(
                    series,
                    period,
                    f"{year:04d}-M{min(month, 12):02d}",
                    value,
                    "source_dash" if value is None else None,
                    tuple(footnotes),
                )
                if "latest" in row:
                    if row["latest"] != "true" or hinted or month == 13:
                        raise ValueError("Invalid latest response hint")
                    hinted = True
                    if window.start <= period < window.end:
                        hints.append((series, period))
                if month == 13:
                    annual_count += 1
                elif window.start <= period < window.end:
                    result.append(observation)
        return (
            tuple(sorted(result, key=lambda obs: (obs.series.value, obs.month))),
            annual_count,
            tuple(messages),
            tuple(sorted(hints)),
        )
    except ValueError, TypeError, KeyError, OverflowError, RecursionError:
        raise MacroError(MacroErrorCode.INVALID_PAYLOAD) from None


class BlsClient(MacroTransport):
    def fetch(self, window: MonthlyWindow, *, deadline: float) -> ProviderRead:
        first, last = request_years(window)
        payload, started, received = self._request(
            "POST",
            BLS_URL,
            max_bytes=MAX_RESPONSE_BYTES,
            deadline=deadline,
            json_body={
                "seriesid": [series.value for series in BLS_SERIES],
                "startyear": str(first),
                "endyear": str(last),
            },
        )
        rows, annual, messages, hints = parse_bls(payload, window)
        self.check_deadline(deadline)
        return ProviderRead(window, rows, started, received, annual, messages, hints)
