"""Opt-in full-release H.15 XML check; no database, saved dataset or model calls."""

import argparse
import time
from datetime import date

import httpx

from market_intelligence.macro.fed import FedClient
from market_intelligence.macro.models import CATALOG, MacroSeries, MonthlyWindow


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2024)
    args = parser.parse_args()
    window = MonthlyWindow(date(args.year, 1, 1), date(args.year + 1, 1, 1))
    with httpx.Client(follow_redirects=False, trust_env=False) as http:
        result = FedClient(http).fetch(window, deadline=time.monotonic() + 120)
    series = MacroSeries.FED_FUNDS
    print(f"{series.value}: {len(result.observations)} returned months; {CATALOG[series].unit}")
    if result.observations:
        first, last = result.observations[0], result.observations[-1]
        print(f"Native month-end labels: {first.native_period} through {last.native_period}")
    print(f"Release prepared text: {result.prepared_text}; no per-observation publication time")
    print(f"Source: Federal Reserve Board H.15; accessed {result.received_at.date().isoformat()}")


if __name__ == "__main__":
    main()
