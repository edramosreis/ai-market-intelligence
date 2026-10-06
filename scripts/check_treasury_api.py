"""Opt-in read-only Treasury HTTP pagination/evidence check against fresh source samples."""

import argparse
import json
import time
from decimal import Decimal, localcontext

import httpx
from pydantic import ValidationError

from market_intelligence.treasury.client import TreasuryClient
from market_intelligence.treasury.models import TreasuryError, TreasuryMonth, TreasuryTenor
from market_intelligence.treasury.query_models import TreasuryCurvePage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    try:
        with (
            httpx.Client(base_url=args.base_url, timeout=30, trust_env=False) as api,
            httpx.Client(
                timeout=30,
                trust_env=False,
                follow_redirects=False,
            ) as http,
        ):
            api.get("/health/ready").raise_for_status()
            source = TreasuryClient(http)
            for month in (TreasuryMonth(2020, 1), TreasuryMonth(2024, 1)):
                observations = source.fetch_month(month, deadline=time.monotonic() + 60)
                expected = {curve.observed_on: curve for curve in observations}
                seen = set()
                cursor = None
                pages = 0
                benchmarks = 0
                while True:
                    params = {
                        "start": month.start.isoformat(),
                        "end": month.end.isoformat(),
                        "limit": "7",
                    }
                    if cursor:
                        params["cursor"] = cursor
                    response = api.get("/v1/treasury/curves", params=params)
                    response.raise_for_status()
                    page = TreasuryCurvePage.model_validate_json(response.content)
                    if (
                        page.coverage.observed_dates != len(expected)
                        or page.coverage.publication_calendar_completeness != "not_established"
                    ):
                        raise ValueError("Unexpected stored-date evidence")
                    pages += 1
                    for curve in page.curves:
                        if (
                            curve.observed_on in seen
                            or curve.observed_on not in expected
                            or curve.status != "stored"
                        ):
                            raise ValueError("Unexpected stored curve")
                        seen.add(curve.observed_on)
                        source_rates = {
                            rate.tenor: rate for rate in expected[curve.observed_on].rates
                        }
                        for rate in curve.rates:
                            original = source_rates[rate.tenor]
                            if (
                                rate.yield_percent != original.yield_percent
                                or rate.missing_reason != original.missing_reason
                            ):
                                raise ValueError("Stored value differs from current source sample")
                        short = source_rates[TreasuryTenor.TWO_YEARS].yield_percent
                        long = source_rates[TreasuryTenor.TEN_YEARS].yield_percent
                        if short is not None and long is not None:
                            with localcontext() as context:
                                context.prec = 50
                                if (
                                    curve.spread.percentage_points != long - short
                                    or curve.spread.basis_points != (long - short) * Decimal(100)
                                ):
                                    raise ValueError("Incorrect stored yield spread")
                            benchmarks += 2
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                    if pages >= 10:
                        raise ValueError("Pagination did not terminate")
                if seen != set(expected) or not expected:
                    raise ValueError("Source dates are not all stored")
                print(
                    json.dumps(
                        {
                            "month": month.provider_month,
                            "source_dates": len(expected),
                            "matched_rates": len(expected) * 14,
                            "matched_benchmarks": benchmarks,
                            "pages": pages,
                            "database_writes": False,
                            "model_calls": False,
                        }
                    )
                )
    except httpx.HTTPError, TreasuryError, ValueError, ValidationError, KeyError:
        print("Treasury API check failed; verify readiness, stored history, and source revisions.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
