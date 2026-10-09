"""Opt-in stored HTTP/reader-SQL parity; no providers, writes or model calls."""

import argparse
import json
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.macro_tables import macro_current as current
from market_intelligence.db.macro_tables import macro_observed_versions as versions
from market_intelligence.db.macro_tables import macro_version_footnotes as notes
from market_intelligence.macro.models import CATALOG, MacroSeries
from market_intelligence.macro.query_models import month_date


def stored_rows(
    conn: Connection, series: MacroSeries, start: date, end: date
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            sa.select(versions)
            .select_from(
                current.join(
                    versions,
                    sa.and_(
                        current.c.source_code == versions.c.source_code,
                        current.c.series_id == versions.c.series_id,
                        current.c.observation_month == versions.c.observation_month,
                        current.c.version_number == versions.c.version_number,
                    ),
                )
            )
            .where(
                current.c.source_code == CATALOG[series].provider,
                current.c.series_id == series.value,
                current.c.observation_month >= start,
                current.c.observation_month < end,
            )
            .order_by(current.c.observation_month)
        ).mappings()
    ]


def check_content(
    conn: Connection, series: MacroSeries, expected: dict[str, Any], actual: dict[str, Any]
) -> None:
    assert actual["month"] == expected["observation_month"].isoformat()
    assert actual["native_period"] == expected["native_period"]
    assert actual["version_number"] == expected["version_number"]
    if expected["value"] is None:
        assert actual["value"] is None and actual["status"] == "source_missing"
    else:
        assert isinstance(actual["value"], str) and Decimal(actual["value"]) == expected["value"]
        assert actual["status"] == "available"
    assert actual["missing_reason"] == expected["missing_reason"]
    assert actual["provenance"]["content_receipt_id"] == str(expected["ingestion_run_id"])
    native_notes = (
        conn.execute(
            sa.select(notes.c.code, notes.c.text)
            .where(
                notes.c.source_code == CATALOG[series].provider,
                notes.c.series_id == series.value,
                notes.c.observation_month == expected["observation_month"],
                notes.c.version_number == expected["version_number"],
            )
            .order_by(notes.c.ordinal)
        )
        .mappings()
        .all()
    )
    assert actual["footnotes"] == [dict(note) for note in native_notes]


def verify_series(
    http: httpx.Client, conn: Connection, series: MacroSeries, start: date, end: date
) -> dict[str, object]:
    expected = stored_rows(conn, series, start, end)
    assert expected, "No stored observations in requested sample"
    actual: list[dict[str, Any]] = []
    cursor = None
    coverage = None
    pages = 0
    while True:
        response = http.get(
            f"/v1/macro/series/{series.value}/observations",
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 5,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        response.raise_for_status()
        page = response.json()
        assert page["historical_release_vintages"] == "not_established"
        assert page["series"]["unit"] == CATALOG[series].unit
        assert page["series"]["seasonal_adjustment"] == CATALOG[series].seasonal_adjustment
        assert page["coverage"]["publication_calendar_completeness"] == "not_established"
        if coverage is not None:
            assert coverage == page["coverage"], "Coverage changed between sample pages"
        coverage = page["coverage"]
        assert {row["provenance"]["content_receipt_id"] for row in page["observations"]} == {
            receipt["run_id"] for receipt in page["receipts"]
        }
        actual.extend(page["observations"])
        pages += 1
        following = page["next_cursor"]
        if following is None:
            break
        assert following != cursor and pages < 240, "Nonterminating pagination"
        cursor = following
    assert len(expected) == len(actual)
    for stored, output in zip(expected, actual, strict=True):
        check_content(conn, series, stored, output)
    assert coverage is not None
    assert coverage["stored_months"] == len(expected)
    assert coverage["available_values"] == sum(row["value"] is not None for row in expected)
    assert coverage["source_missing_values"] == sum(row["value"] is None for row in expected)
    assert coverage["expected_months"] == (end.year - start.year) * 12 + end.month - start.month
    assert coverage["not_stored_months"] == coverage["expected_months"] - len(expected)
    # Inspect the oldest sample month's local versions, independently of current rows.
    month = expected[0]["observation_month"]
    history = (
        conn.execute(
            sa.select(versions)
            .where(
                versions.c.source_code == CATALOG[series].provider,
                versions.c.series_id == series.value,
                versions.c.observation_month == month,
            )
            .order_by(versions.c.version_number)
        )
        .mappings()
        .all()
    )
    response = http.get(
        f"/v1/macro/series/{series.value}/versions", params={"month": month.isoformat()}
    )
    response.raise_for_status()
    version_page = response.json()
    assert version_page["observed_versions"] == len(history)
    assert version_page["current_version_number"] == expected[0]["version_number"]
    assert version_page["historical_release_vintages"] == "not_established"
    # A bounded default sample must fit its first version page; larger histories need pagination.
    assert len(history) <= 100 and version_page["next_cursor"] is None
    for historical, output in zip(history, version_page["versions"], strict=True):
        check_content(conn, series, dict(historical), output)
    latest_response = http.get(f"/v1/macro/series/{series.value}/latest")
    latest_response.raise_for_status()
    latest = latest_response.json()
    closed_month = month_date(latest["latest_completed_month"])
    all_current = stored_rows(conn, series, CATALOG[series].earliest_month, closed_month)
    closed_row = stored_rows(
        conn,
        series,
        closed_month,
        date(closed_month.year + (closed_month.month == 12), closed_month.month % 12 + 1, 1),
    )
    all_current.extend(closed_row)
    assert all_current and latest["status"] == "stored"
    check_content(conn, series, all_current[-1], latest["observation"])
    last = all_current[-1]["observation_month"]
    assert (
        latest["months_behind_latest_completed"]
        == (closed_month.year - last.year) * 12 + closed_month.month - last.month
    )
    assert latest["publication_delay"] == "not_established"
    return {
        "series_id": series.value,
        "stored_months": len(expected),
        "pages": pages,
        "not_stored_months": coverage["not_stored_months"],
        "native_content_and_versions": "match",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2025-01-01")
    args = parser.parse_args()
    start, end = month_date(args.start), month_date(args.end)
    if start >= end or (end.year - start.year) * 12 + end.month - start.month > 1200:
        raise ValueError("Use an ordered window of at most 1200 months")
    engine = create_db_engine(DatabaseSettings(), DatabaseRole.READ)  # type: ignore[call-arg]
    try:
        with httpx.Client(base_url="http://127.0.0.1:8000", trust_env=False, timeout=30) as http:
            http.get("/health/ready").raise_for_status()
            with (
                engine.connect().execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True
                ) as conn,
                conn.begin(),
            ):
                results = [verify_series(http, conn, series, start, end) for series in MacroSeries]
        print(
            json.dumps({"checks": results, "provider_requests": 0, "writes": 0, "model_calls": 0})
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError, SQLAlchemyError, AssertionError, ValueError:
        raise SystemExit("Stored macro HTTP/reader check failed; details withheld") from None
