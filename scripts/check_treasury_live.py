"""Opt-in public monthly Treasury samples; no configuration, keys, or database."""

import json
import time

import httpx

from market_intelligence.treasury.client import TreasuryClient
from market_intelligence.treasury.models import TreasuryError, TreasuryMonth, TreasuryTenor


def main() -> int:
    try:
        with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as http:
            source = TreasuryClient(http)
            for month in (TreasuryMonth(1990, 1), TreasuryMonth(2020, 1), TreasuryMonth(2024, 1)):
                curves = source.fetch_month(month, deadline=time.monotonic() + 60)
                if not curves:
                    raise ValueError("Sample month is unavailable")
                selected = {TreasuryTenor.TWO_YEARS, TreasuryTenor.TEN_YEARS}
                available = sum(
                    rate.yield_percent is not None
                    for curve in curves
                    for rate in curve.rates
                    if rate.tenor in selected
                )
                if available != len(curves) * len(selected):
                    raise ValueError("Sample benchmark tenors are unavailable")
                print(
                    json.dumps(
                        {
                            "source": "us_treasury",
                            "dataset": "daily_nominal_par_yield_curve",
                            "month": month.provider_month,
                            "source_dates": len(curves),
                            "first_date": curves[0].observed_on.isoformat(),
                            "last_date": curves[-1].observed_on.isoformat(),
                            "benchmark_rates_available": available,
                            "units": "percent",
                            "calendar_coverage_basis": "returned_source_dates",
                        }
                    )
                )
    except (TreasuryError, ValueError) as error:
        print(
            json.dumps(
                {
                    "check_failed": True,
                    "error_code": error.code.value
                    if isinstance(error, TreasuryError)
                    else "sample_unavailable",
                }
            )
        )
        return 1
    print("Treasury samples validated; no database writes, downloaded fixtures, or model calls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
