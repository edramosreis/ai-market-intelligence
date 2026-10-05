"""Opt-in public API smoke check; keeps samples in memory and never accesses PostgreSQL."""

import json
import time
from datetime import timedelta

import httpx

from market_intelligence.ingestion.coinbase import BASE_URL, CoinbaseClient
from market_intelligence.ingestion.models import (
    IngestionError,
    TimeWindow,
    closed_cutoff,
    parse_instant,
    utc_now,
)


def main() -> int:
    complete = True
    try:
        with httpx.Client(
            base_url=BASE_URL, headers={"User-Agent": "market-intelligence/0.1"}
        ) as http:
            source = CoinbaseClient(http)
            source.verify_product(time.monotonic() + 60)
            for day in ("2020-01-01", "2024-01-01"):
                start = parse_instant(day)
                window = TimeWindow(start, start + timedelta(hours=1))
                observations = source.fetch_chunk(
                    window, closed_cutoff(utc_now()), time.monotonic() + 60
                )
                missing = window.expected - len(observations)
                complete = complete and missing == 0
                print(
                    json.dumps(
                        {
                            "start": window.start.isoformat(),
                            "end": window.end.isoformat(),
                            "expected": window.expected,
                            "validated": len(observations),
                            "missing": missing,
                            "authenticated": False,
                            "database_writes": False,
                        }
                    ),
                    flush=True,
                )
    except IngestionError as error:
        print(json.dumps({"event": "live_check_failed", "code": error.code.value}), flush=True)
        return 1
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
