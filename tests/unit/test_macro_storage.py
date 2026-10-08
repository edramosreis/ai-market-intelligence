from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.db.macro_store import MacroStore
from market_intelligence.macro.models import (
    MacroProvider,
    MacroSeries,
    MonthlyObservation,
    MonthlyWindow,
    ProviderRead,
)
from market_intelligence.macro.storage_models import (
    MacroStorageError,
    validate_read,
    validate_window,
)

NOW = datetime(2026, 10, 8, tzinfo=UTC)
WINDOW = MonthlyWindow(date(2024, 1, 1), date(2025, 1, 1))
ROW = MonthlyObservation(MacroSeries.CPI, WINDOW.start, "2024-M01", Decimal("10.125"))
READ = ProviderRead(WINDOW, (ROW,), NOW, NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"annual_average_count": True},
        {"annual_average_count": -1},
        {"annual_average_count": 21},
        {"source_messages": ("x" * 4001,)},
        {"prepared_text": "2024-01-01T00:00:00"},
        {"source_annotations": (("Description", "Unverified BLS metadata"),)},
        {"latest_hints": ((MacroSeries.CPI, date(2024, 2, 1)),)},
        {"latest_hints": ((MacroSeries.CPI, WINDOW.start), (MacroSeries.CPI, WINDOW.start))},
    ],
)
def test_invalid_read_metadata_is_rejected_before_database_access(changes: dict[str, Any]) -> None:
    engine = Mock(spec=Engine)
    read = replace(READ, **changes)
    with pytest.raises(MacroStorageError, match="^invalid_read$"):
        MacroStore(engine).persist(uuid4(), MacroProvider.BLS, read, NOW)
    engine.begin.assert_not_called()


def test_wrong_publisher_and_unverified_fed_prepared_text_fail() -> None:
    row = MonthlyObservation(MacroSeries.FED_FUNDS, WINDOW.start, "2024-01-31", Decimal("0"))
    read = replace(READ, observations=(row,), prepared_text="2026-10-07T15:40:04")
    validate_read(MacroProvider.FED, read)
    with pytest.raises(MacroStorageError, match="^invalid_read$"):
        validate_read(MacroProvider.BLS, read)
    changes: list[dict[str, Any]] = [
        {"prepared_text": None},
        {"prepared_text": "2026-10-07T15:40:04Z"},
        {"prepared_text": "2026-02-30T15:40:04"},
        {"annual_average_count": 1},
        {"source_messages": ("Unexpected Fed metadata",)},
        {"source_annotations": (("Description", "First"), ("Description", "Second"))},
    ]
    for change in changes:
        with pytest.raises(MacroStorageError, match="^invalid_read$"):
            validate_read(MacroProvider.FED, replace(read, **change))


def test_window_boundaries_and_capacity_are_explicit() -> None:
    assert validate_window(MacroProvider.BLS, WINDOW) == 24
    assert validate_window(MacroProvider.FED, WINDOW) == 12
    for start, end in [(date(1946, 1, 1), date(1947, 1, 1)), (date(2020, 2, 1), date(2030, 2, 1))]:
        engine = Mock(spec=Engine)
        with pytest.raises(ValueError):
            MacroStore(engine).start_run(MacroProvider.BLS, MonthlyWindow(start, end), NOW)
        engine.begin.assert_not_called()
    with pytest.raises(ValueError):
        validate_window(MacroProvider.FED, MonthlyWindow(date(1954, 7, 1), date(2054, 8, 1)))


def test_connection_errors_expose_only_a_controlled_code() -> None:
    engine = Mock(spec=Engine)
    engine.begin.side_effect = SQLAlchemyError("Private connection credentials or SQL")
    with pytest.raises(MacroStorageError, match="^database_error$"):
        MacroStore(engine).start_run(MacroProvider.BLS, WINDOW, NOW)
