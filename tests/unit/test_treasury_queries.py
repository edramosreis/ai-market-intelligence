import base64
import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine

from market_intelligence.config import ApiSettings
from market_intelligence.queries.models import QueryValidationError
from market_intelligence.treasury.queries import TreasuryQueries
from market_intelligence.treasury.query_models import treasury_cursor, treasury_cursor_after

START, END = date(2024, 1, 1), date(2024, 2, 1)
NOW = datetime(2026, 10, 6, tzinfo=UTC)


def test_cursor_binds_source_dataset_date_bounds_and_after() -> None:
    cursor = treasury_cursor(START, END, date(2024, 1, 2))
    assert treasury_cursor_after(cursor, START, END) == date(2024, 1, 2)
    with pytest.raises(QueryValidationError):
        treasury_cursor_after(cursor, START, date(2024, 3, 1))


@pytest.mark.parametrize(
    "cursor",
    [
        "!invalid",
        "",
        "a" * 1025,
        base64.urlsafe_b64encode(b"{}").decode(),
        base64.urlsafe_b64encode(
            json.dumps(
                [
                    True,
                    "us_treasury",
                    "daily_nominal_par_yield_curve",
                    "2024-01-01",
                    "2024-02-01",
                    "2024-01-02",
                ]
            ).encode()
        ).decode(),
        treasury_cursor(START, END, END),
    ],
)
def test_invalid_cursor_is_rejected(cursor: str) -> None:
    with pytest.raises(QueryValidationError):
        treasury_cursor_after(cursor, START, END)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (END, START),
        (START, START),
        (datetime(2024, 1, 1), END),
        (date(1989, 12, 31), START),
        (START, date(2027, 1, 1)),
        (START, date(2024, 3, 1)),
    ],
)
def test_date_validation_happens_before_database_access(start: date, end: date) -> None:
    # An engine with no connection is sufficient; these invalid requests must not touch PostgreSQL.
    engine = create_engine("postgresql+psycopg://unused:unused@127.0.0.1:1/unused")
    try:
        queries = TreasuryQueries(engine, ApiSettings(max_window_days=31), now=lambda: NOW)
        with pytest.raises(QueryValidationError):
            queries.curve_page(start, end)
    finally:
        engine.dispose()
