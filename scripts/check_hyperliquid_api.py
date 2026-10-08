"""Opt-in funding HTTP/source parity and independently stored OI receipt checks."""

import time
from datetime import UTC, datetime
from decimal import Decimal, localcontext

import httpx
import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.tables import open_interest_snapshots
from market_intelligence.hyperliquid.client import HyperliquidClient
from market_intelligence.hyperliquid.models import FundingWindow
from market_intelligence.hyperliquid.query_models import (
    FundingPage,
    FundingSummary,
    OpenInterestLatest,
)

BASE_URL = "http://127.0.0.1:8000"


def main() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 2, 1, tzinfo=UTC)
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as http:
        source = HyperliquidClient(http)
        reference = source.fetch_funding(FundingWindow(start, end), deadline=time.monotonic() + 60)
        observed = []
        cursor = None
        pages = 0
        while True:
            params: dict[str, str | int] = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            response = http.get(BASE_URL + "/v1/hyperliquid/funding", params=params)
            response.raise_for_status()
            page = FundingPage.model_validate(response.json())
            assert (
                page.instrument.code == "BTC-PERP" and page.instrument.denomination_asset == "USDT"
            )
            assert page.funding_rate_unit == "fraction_per_hour"
            assert page.coverage.observed_hours == len(reference)
            observed.extend(page.events)
            cursor = page.next_cursor
            pages += 1
            assert pages <= 5, "Unexpected funding pagination"
            if cursor is None:
                break
        assert len(observed) == len(reference), "Stored funding differs from fresh sample"
        assert [
            (row.event_at, row.settlement_hour, row.funding_rate, row.premium) for row in observed
        ] == [
            (row.event_at, row.settlement_hour, row.funding_rate, row.premium) for row in reference
        ], "Funding values/timestamps differ"
        response = http.get(
            BASE_URL + "/v1/hyperliquid/funding/summary",
            params={"start": start.isoformat(), "end": end.isoformat()},
        )
        response.raise_for_status()
        summary = FundingSummary.model_validate(response.json())
        assert summary.coverage.expected_hours == 744 and summary.coverage.status == "complete"
        with localcontext() as context:
            context.prec = 60
            total = sum((row.funding_rate for row in reference), Decimal(0))
            assert summary.rate_sum == total and summary.rate_sum_percent == total * 100
        response = http.get(BASE_URL + "/v1/hyperliquid/open-interest/latest")
        response.raise_for_status()
        latest = OpenInterestLatest.model_validate(response.json())
        assert latest.snapshot is not None, "Collect one OI snapshot before this check"
        engine = create_db_engine(DatabaseSettings(), DatabaseRole.READ)  # type: ignore[call-arg]
        try:
            with engine.connect() as conn:
                row = (
                    conn.execute(
                        sa.select(open_interest_snapshots).where(
                            open_interest_snapshots.c.snapshot_id == latest.snapshot.snapshot_id
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["received_at"] == latest.snapshot.received_at
                assert row["fetch_started_at"] == latest.snapshot.fetch_started_at
                assert row["open_interest_btc"] == latest.snapshot.open_interest_btc
                assert row["mark_price_usdt"] == latest.snapshot.mark_price_usdt
                assert row["oracle_price_usdt"] == latest.snapshot.oracle_price_usdt
                assert row["ingestion_run_id"] == latest.snapshot.ingestion_run_id
                assert latest.snapshot.source_event_at is None
        finally:
            engine.dispose()
        print(
            f"January 2024: {len(observed)} funding events/rates/premiums/slots "
            "matched fresh source "
            f"through {pages} HTTP pages; rate sum {total} fraction ({total * 100} percent)."
        )
        print(
            "Latest OI HTTP evidence matches the stored receipt identity, timing, "
            "quantities, and prices."
        )
        print("Reader-only checks; no ingestion, archive access, or model calls.")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError, SQLAlchemyError:
        raise SystemExit(
            "Hyperliquid check failed; verify provider/API/database availability."
        ) from None
