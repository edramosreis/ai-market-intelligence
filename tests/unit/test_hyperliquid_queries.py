"""Continuation binding and strict native time validation without database access."""

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from market_intelligence.hyperliquid.query_models import cursor_after, cursor_for
from market_intelligence.queries.models import QueryValidationError

START = datetime(2024, 1, 1, tzinfo=UTC)
END = START + timedelta(days=1)


def test_cursors_preserve_source_offsets_and_snapshot_identity() -> None:
    instant = START + timedelta(milliseconds=76)
    cursor = cursor_for("funding", START, END, instant)
    assert cursor_after(cursor, "funding", START, END) == (instant, None)
    identity = uuid4()
    cursor = cursor_for("open_interest", START, END, instant, identity)
    assert cursor_after(cursor, "open_interest", START, END) == (instant, identity)
    assert cursor_after(None, "funding", START, END) is None


@pytest.mark.parametrize(
    "change", ["window", "dataset", "version", "time", "identity", "field-count"]
)
def test_tampered_cursors_rejected(change: str) -> None:
    values = [
        1,
        "hyperliquid",
        "BTC-PERP",
        "open_interest",
        START.isoformat(),
        END.isoformat(),
        START.isoformat(),
        str(uuid4()),
    ]
    if change == "window":
        values[4] = (START + timedelta(hours=1)).isoformat()
    elif change == "dataset":
        values[3] = "funding"
    elif change == "version":
        values[0] = 2
    elif change == "time":
        values[6] = END.isoformat()
    elif change == "identity":
        values[7] = 1
    else:
        values.pop()
    cursor = (
        base64.urlsafe_b64encode(json.dumps(values, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    with pytest.raises(QueryValidationError):
        cursor_after(cursor, "open_interest", START, END)


@pytest.mark.parametrize(
    "cursor", ["", "%%%", "null", "x" * 1025], ids=["empty", "bad-base64", "not-json", "oversized"]
)
def test_malformed_cursors(cursor: str) -> None:
    with pytest.raises(QueryValidationError):
        cursor_after(cursor, "funding", START, END)
