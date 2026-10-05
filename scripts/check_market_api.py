"""Opt-in HTTP smoke check against an already backfilled local database."""

import argparse
import json

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    try:
        with httpx.Client(base_url=args.base_url, timeout=30) as client:
            for endpoint in ("/health/live", "/health/ready"):
                client.get(endpoint).raise_for_status()
            response = client.get("/v1/markets")
            response.raise_for_status()
            market = next(
                item
                for item in response.json()
                if item["source_code"] == "coinbase_exchange"
                and item["source_product_id"] == "BTC-USD"
            )
            prefix = f"/v1/markets/{market['id']}"
            for start, end in [("2020-01-01", "2020-01-02"), ("2024-01-01", "2024-01-02")]:
                summary = client.get(prefix + "/summary", params={"start": start, "end": end})
                summary.raise_for_status()
                evidence = summary.json()
                if evidence["coverage"]["status"] != "complete":
                    raise ValueError("Sampled day is incomplete in the local database")
                hourly = client.get(
                    prefix + "/candles",
                    params={
                        "start": start,
                        "end": end,
                        "interval_seconds": 3600,
                    },
                )
                hourly.raise_for_status()
                bars = hourly.json()["candles"]
                if len(bars) != 24 or any(bar["status"] != "complete" for bar in bars):
                    raise ValueError("Sampled day lacks 24 complete derived hourly bars")
                print(
                    json.dumps(
                        {
                            "start": start,
                            "expected": 288,
                            "actual": evidence["coverage"]["actual_buckets"],
                            "hourly_bars": len(bars),
                            "database_writes": False,
                        }
                    )
                )
            latest = client.get(prefix + "/latest")
            latest.raise_for_status()
            evidence = latest.json()
            print(
                json.dumps(
                    {
                        "latest_status": evidence["status"],
                        "stale": evidence["stale"],
                        "age_seconds": evidence["age_seconds"],
                        "database_writes": False,
                    }
                )
            )
    except httpx.HTTPError, ValueError, KeyError, StopIteration:
        print("API check failed; verify readiness and the documented historical backfill.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
