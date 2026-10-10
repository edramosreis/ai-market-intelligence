"""Opt-in, one-year keyless source check; no database, saved dataset or model calls."""

import argparse
import time
from datetime import date

import httpx

from market_intelligence.macro.bls import BLS_SERIES, BlsClient
from market_intelligence.macro.models import BLS_NOTICE, CATALOG, MonthlyWindow


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2024)
    args = parser.parse_args()
    window = MonthlyWindow(date(args.year, 1, 1), date(args.year + 1, 1, 1))
    with httpx.Client(follow_redirects=False, trust_env=False) as http:
        result = BlsClient(http).fetch(window, deadline=time.monotonic() + 60)
    for series in BLS_SERIES:
        rows = [row for row in result.observations if row.series == series]
        available = sum(row.value is not None for row in rows)
        print(
            f"{series.value}: {len(rows)} returned months, {available} available; "
            f"{CATALOG[series].unit}"
        )
    print(
        f"Annual averages excluded: {result.annual_average_count}; "
        f"source messages: {len(result.source_messages)}"
    )
    print(f"Source: BLS.gov; accessed {result.received_at.date().isoformat()}")
    print(BLS_NOTICE)


if __name__ == "__main__":
    main()
