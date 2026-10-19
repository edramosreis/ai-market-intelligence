"""The existing timestamp contract applies independently of any provider."""

from datetime import UTC, datetime

import pytest

from market_intelligence.time_utils import parse_instant

START = datetime(2024, 1, 1, tzinfo=UTC)


def test_cli_date_shorthand_and_explicit_offset() -> None:
    assert parse_instant("2024-01-01") == START
    assert parse_instant("2024-01-01T03:00:00+03:00") == START
    with pytest.raises(ValueError):
        parse_instant("2024-01-01T00:00:00")
