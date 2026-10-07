"""Opt-in public funding/context samples; no database, archive, key, or model call."""

import time
from datetime import UTC, datetime, timedelta

import httpx

from market_intelligence.hyperliquid.client import HyperliquidClient
from market_intelligence.hyperliquid.models import FundingWindow


def main() -> None:
    with httpx.Client(follow_redirects=False, trust_env=False) as http:
        client = HyperliquidClient(http)
        for days in (1, 31):
            start = datetime(2024, 1, 1, tzinfo=UTC)
            values = client.fetch_funding(
                FundingWindow(start, start + timedelta(days=days)), deadline=time.monotonic() + 60
            )
            slots = {row.settlement_hour for row in values}
            print(
                f"BTC funding 2024-01 ({days} days): {len(values)} events, "
                f"{len(slots)} distinct UTC settlement hours; exact source milliseconds retained."
            )
        snapshot = client.fetch_open_interest(deadline=time.monotonic() + 30)
        print(
            f"BTC OI: {snapshot.open_interest_btc} BTC; mark {snapshot.mark_price_usdt} USDT; "
            f"oracle {snapshot.oracle_price_usdt} USDT; "
            f"received {snapshot.received_at.isoformat()}."
        )
        print(
            "Observed public samples only. OI uses local receipt time, "
            "with no source event timestamp."
        )


if __name__ == "__main__":
    main()
