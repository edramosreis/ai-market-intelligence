from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.macro_store import MACRO_LOCK_KEYS, MacroStore
from market_intelligence.db.macro_tables import (
    macro_current as current,
)
from market_intelligence.db.macro_tables import (
    macro_ingestion_runs as runs,
)
from market_intelligence.db.macro_tables import (
    macro_observed_versions as versions,
)
from market_intelligence.db.macro_tables import (
    macro_version_footnotes as notes,
)
from market_intelligence.macro.models import (
    Footnote,
    MacroProvider,
    MacroSeries,
    MonthlyObservation,
    MonthlyWindow,
    ProviderRead,
)
from market_intelligence.macro.storage_models import (
    MacroFailureCode,
    MacroStorageError,
    MacroWriteReport,
)

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 8, tzinfo=UTC)
WINDOW = MonthlyWindow(date(2024, 1, 1), date(2024, 4, 1))


def observation(
    series: MacroSeries = MacroSeries.CPI,
    month: int = 1,
    value: str | None = "10.125",
    footnotes: tuple[Footnote, ...] = (),
) -> MonthlyObservation:
    return MonthlyObservation(
        series,
        date(2024, month, 1),
        f"2024-M{month:02d}",
        None if value is None else Decimal(value),
        "source_dash" if value is None else None,
        footnotes,
    )


def received(
    rows: tuple[MonthlyObservation, ...],
    seconds: int = 0,
    *,
    annual_average_count: int = 0,
    source_messages: tuple[str, ...] = (),
    latest_hints: tuple[tuple[MacroSeries, date], ...] = (),
    prepared_text: str | None = None,
    source_annotations: tuple[tuple[str, str], ...] = (),
) -> ProviderRead:
    return ProviderRead(
        WINDOW,
        tuple(sorted(rows, key=lambda row: (row.series.value, row.month))),
        NOW + timedelta(seconds=seconds + 1),
        NOW + timedelta(seconds=seconds + 2),
        annual_average_count,
        source_messages,
        latest_hints,
        prepared_text,
        source_annotations,
    )


def load(
    store: MacroStore, read: ProviderRead, provider: MacroProvider = MacroProvider.BLS
) -> MacroWriteReport:
    identity = store.start_run(provider, read.window, read.fetch_started_at - timedelta(seconds=1))
    return store.persist(identity, provider, read, read.received_at + timedelta(seconds=1))


def counts(engine: Engine) -> tuple[int, int, int]:
    with engine.connect() as conn:
        totals = [
            conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
            for table in (current, versions, notes)
        ]
        return totals[0], totals[1], totals[2]


def test_initial_exact_values_dash_zero_and_atomic_audit(macro_store: MacroStore) -> None:
    rows = (
        observation(),
        observation(MacroSeries.UNEMPLOYMENT, value="0"),
        observation(month=2, value=None, footnotes=(Footnote("X", "Synthetic unavailable"),)),
    )
    report = load(
        macro_store, received(rows, annual_average_count=2, source_messages=("Synthetic message",))
    )
    assert (
        report.received,
        report.inserted,
        report.corrected,
        report.unchanged,
        report.source_missing,
        report.retained,
        report.versions_written,
    ) == (3, 3, 0, 0, 1, 0, 3)
    assert counts(macro_store.engine) == (3, 3, 1)
    with macro_store.engine.connect() as conn:
        audit = conn.execute(sa.select(runs)).mappings().one()
        assert audit["status"] == "succeeded" and audit["inserted"] == 3
        assert (
            audit["source_messages"] == ["Synthetic message"] and audit["annual_average_count"] == 2
        )
        data = (
            conn.execute(
                sa.select(versions).order_by(versions.c.series_id, versions.c.observation_month)
            )
            .mappings()
            .all()
        )
        assert [row["value"] for row in data] == [Decimal("10.125"), None, Decimal("0")]
        assert data[1]["missing_reason"] == "source_dash" and data[0]["native_period"] == "2024-M01"
        assert all(row["ingestion_run_id"] == report.run_id for row in data)
        assert audit["fetch_started_at"] == NOW + timedelta(seconds=1)
        assert audit["received_at"] == NOW + timedelta(seconds=2)
        assert audit["finished_at"] == data[0]["materialized_at"] == NOW + timedelta(seconds=3)


def test_unchanged_replay_preserves_versions_provenance_and_ignores_response_hints(
    macro_store: MacroStore,
) -> None:
    first = load(macro_store, received((observation(),)))
    second = load(
        macro_store,
        received(
            (observation(value="10.1250"),), 10, latest_hints=((MacroSeries.CPI, WINDOW.start),)
        ),
    )
    assert (second.unchanged, second.versions_written) == (1, 0) and counts(macro_store.engine) == (
        1,
        1,
        0,
    )
    with macro_store.engine.connect() as conn:
        version = conn.execute(sa.select(versions)).mappings().one()
        assert version["ingestion_run_id"] == first.run_id and version[
            "materialized_at"
        ] == NOW + timedelta(seconds=3)
        assert conn.execute(
            sa.select(runs.c.latest_hints).where(runs.c.id == second.run_id)
        ).scalar_one() == [{"series_id": "CUSR0000SA0", "month": "2024-01-01"}]


def test_value_footnote_and_missing_corrections_keep_every_locally_observed_state(
    macro_store: MacroStore,
) -> None:
    original = observation(footnotes=(Footnote("A", "Original synthetic footnote"),))
    reads = [
        original,
        replace(original, value=Decimal("11")),
        replace(
            original,
            value=Decimal("11"),
            footnotes=(Footnote("A", "Corrected synthetic footnote"),),
        ),
        observation(value=None, footnotes=(Footnote("X", "Synthetic unavailable"),)),
        original,
    ]
    reports = [load(macro_store, received((row,), index * 10)) for index, row in enumerate(reads)]
    assert reports[0].inserted == 1 and all(report.corrected == 1 for report in reports[1:])
    with macro_store.engine.connect() as conn:
        history = (
            conn.execute(sa.select(versions).order_by(versions.c.version_number)).mappings().all()
        )
        assert [row["version_number"] for row in history] == [1, 2, 3, 4, 5]
        assert [row["value"] for row in history] == [
            Decimal("10.125"),
            Decimal("11"),
            Decimal("11"),
            None,
            Decimal("10.125"),
        ]
        assert [row["ingestion_run_id"] for row in history] == [report.run_id for report in reports]
        assert conn.execute(sa.select(current.c.version_number)).scalar_one() == 5
        assert (
            conn.execute(sa.select(notes.c.text).where(notes.c.version_number == 1)).scalar_one()
            == "Original synthetic footnote"
        )


def test_footnote_order_alone_preserves_current_version(macro_store: MacroStore) -> None:
    row = observation(footnotes=(Footnote("A", "First"), Footnote("B", "Second")))
    load(macro_store, received((row,)))
    report = load(
        macro_store, received((replace(row, footnotes=tuple(reversed(row.footnotes))),), 10)
    )
    assert report.unchanged == 1 and counts(macro_store.engine) == (1, 1, 2)


def test_omitted_months_and_empty_series_are_retained_with_exact_audit_periods(
    macro_store: MacroStore,
) -> None:
    load(
        macro_store,
        received((observation(), observation(month=2), observation(MacroSeries.UNEMPLOYMENT))),
    )
    report = load(macro_store, received((observation(),), 10))
    assert report.retained_periods == (
        (MacroSeries.CPI, date(2024, 2, 1)),
        (MacroSeries.UNEMPLOYMENT, WINDOW.start),
    )
    assert report.unchanged == 1 and report.retained == 2
    with macro_store.engine.connect() as conn:
        assert conn.execute(
            sa.select(runs.c.retained_periods).where(runs.c.id == report.run_id)
        ).scalar_one() == [
            {"series_id": "CUSR0000SA0", "month": "2024-02-01"},
            {"series_id": "LNS14000000", "month": "2024-01-01"},
        ]
    empty = load(macro_store, received((), 20))
    assert empty.received == 0 and empty.retained == 3 and counts(macro_store.engine) == (3, 3, 0)


def test_fed_prepared_and_annotations_remain_receipt_metadata(macro_store: MacroStore) -> None:
    row = MonthlyObservation(MacroSeries.FED_FUNDS, WINDOW.start, "2024-01-31", Decimal("1.25"))
    first = load(
        macro_store,
        received(
            (row,),
            prepared_text="2026-10-07T15:40:04",
            source_annotations=(("Long Description", "Synthetic monthly rate"),),
        ),
        MacroProvider.FED,
    )
    second = load(
        macro_store,
        received(
            (row,),
            10,
            prepared_text="2026-10-08T15:40:04",
            source_annotations=(("Long Description", "New synthetic description"),),
        ),
        MacroProvider.FED,
    )
    assert second.unchanged == 1 and counts(macro_store.engine) == (1, 1, 0)
    with macro_store.engine.connect() as conn:
        assert conn.execute(sa.select(versions.c.ingestion_run_id)).scalar_one() == first.run_id
        audit = conn.execute(sa.select(runs).where(runs.c.id == second.run_id)).mappings().one()
        assert audit["prepared_text"] == "2026-10-08T15:40:04"
        assert audit["source_annotations"] == [["Long Description", "New synthetic description"]]


def test_late_older_receipt_cannot_overwrite_a_newer_read(macro_store: MacroStore) -> None:
    old = received((observation(value="9"),))
    old_id = macro_store.start_run(MacroProvider.BLS, WINDOW, NOW)
    newer = load(macro_store, received((observation(value="12"),), 10))
    with pytest.raises(MacroStorageError, match="^stale_read$"):
        macro_store.persist(old_id, MacroProvider.BLS, old, NOW + timedelta(seconds=30))
    macro_store.fail_run(old_id, MacroFailureCode.STALE_READ, NOW + timedelta(seconds=30))
    assert counts(macro_store.engine) == (1, 1, 0)
    with macro_store.engine.connect() as conn:
        assert conn.execute(sa.select(versions.c.ingestion_run_id)).scalar_one() == newer.run_id
        assert (
            conn.execute(sa.select(runs.c.error_code).where(runs.c.id == old_id)).scalar_one()
            == "stale_read"
        )


def test_unchanged_receipt_still_prevents_delayed_older_corrections(
    macro_store: MacroStore,
) -> None:
    first = load(macro_store, received((observation(),)))
    delayed = received((observation(value="9"),), 10)
    identity = macro_store.start_run(MacroProvider.BLS, WINDOW, NOW + timedelta(seconds=10))
    newer = load(macro_store, received((observation(),), 20))
    assert newer.unchanged == 1
    with pytest.raises(MacroStorageError, match="^stale_read$"):
        macro_store.persist(identity, MacroProvider.BLS, delayed, NOW + timedelta(seconds=30))
    with macro_store.engine.connect() as conn:
        assert conn.execute(sa.select(versions.c.ingestion_run_id)).scalar_one() == first.run_id
    assert counts(macro_store.engine) == (1, 1, 0)


def test_delayed_disjoint_receipts_and_omission_counts_are_scoped_to_the_window(
    macro_store: MacroStore,
) -> None:
    january = MonthlyWindow(WINDOW.start, date(2024, 2, 1))
    february = MonthlyWindow(january.end, date(2024, 3, 1))
    delayed = replace(received((observation(),)), window=january)
    identity = macro_store.start_run(MacroProvider.BLS, january, NOW)
    load(macro_store, replace(received((observation(month=2),), 10), window=february))
    report = macro_store.persist(identity, MacroProvider.BLS, delayed, NOW + timedelta(seconds=20))
    assert report.inserted == 1 and report.retained == 0
    omitted = load(macro_store, replace(received((), 30), window=january))
    assert omitted.retained_periods == ((MacroSeries.CPI, WINDOW.start),)
    assert counts(macro_store.engine) == (2, 2, 0)


def test_reader_repeatable_snapshot_keeps_current_content_and_receipt_consistent(
    macro_store: MacroStore,
    database_settings: DatabaseSettings,
) -> None:
    first = load(macro_store, received((observation(),)))
    statement = sa.select(versions.c.value, versions.c.version_number, runs.c.id).select_from(
        current.join(versions).join(runs)
    )
    reader = create_db_engine(database_settings, DatabaseRole.READ)
    try:
        with (
            reader.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
            conn.begin(),
        ):
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            before = conn.execute(statement).one()
            corrected = load(macro_store, received((observation(value="11"),), 10))
            assert conn.execute(statement).one() == before == (Decimal("10.125"), 1, first.run_id)
        with reader.connect() as conn:
            assert conn.execute(statement).one() == (Decimal("11"), 2, corrected.run_id)
    finally:
        reader.dispose()


def test_atomic_failure_rolls_back_versions_notes_pointers_and_success_counts(
    macro_store: MacroStore,
) -> None:
    first = load(macro_store, received((observation(),)))
    read = received(
        (
            observation(value="11", footnotes=(Footnote("A", "Synthetic new note"),)),
            observation(month=2),
        ),
        10,
    )
    run_id = macro_store.start_run(MacroProvider.BLS, WINDOW, NOW + timedelta(seconds=10))

    def fail_audit(*args: object) -> None:
        if str(args[2]).startswith("UPDATE macro_ingestion_runs"):
            raise SQLAlchemyError("Private synthetic SQL details")

    sa.event.listen(macro_store.engine, "before_cursor_execute", fail_audit)
    try:
        with pytest.raises(MacroStorageError, match="^database_error$"):
            macro_store.persist(run_id, MacroProvider.BLS, read, NOW + timedelta(seconds=13))
    finally:
        sa.event.remove(macro_store.engine, "before_cursor_execute", fail_audit)
    assert counts(macro_store.engine) == (1, 1, 0)
    with macro_store.engine.connect() as conn:
        assert conn.execute(sa.select(versions.c.ingestion_run_id)).scalar_one() == first.run_id
        run = conn.execute(sa.select(runs).where(runs.c.id == run_id)).mappings().one()
        assert run["status"] == "running" and run["received"] == 0
    macro_store.fail_run(run_id, MacroFailureCode.DATABASE_ERROR, NOW + timedelta(seconds=14))


def test_terminal_unknown_and_mismatched_runs_fail_without_writes(macro_store: MacroStore) -> None:
    read = received((observation(),))
    first = load(macro_store, read)
    for identity in (first.run_id, uuid4()):
        with pytest.raises(MacroStorageError, match="^invalid_run$"):
            macro_store.persist(identity, MacroProvider.BLS, read, NOW + timedelta(seconds=3))
        with pytest.raises(MacroStorageError, match="^invalid_run$"):
            macro_store.fail_run(identity, MacroFailureCode.HTTP_ERROR, NOW + timedelta(seconds=4))
    wrong = macro_store.start_run(
        MacroProvider.BLS, MonthlyWindow(date(2024, 2, 1), date(2024, 3, 1)), NOW
    )
    with pytest.raises(MacroStorageError, match="^invalid_run$"):
        macro_store.persist(wrong, MacroProvider.BLS, read, NOW + timedelta(seconds=3))
    assert counts(macro_store.engine) == (1, 1, 0)


def test_provider_write_locks_are_independent_and_released(
    macro_store: MacroStore, admin_engine: Engine
) -> None:
    identity = macro_store.start_run(MacroProvider.BLS, WINDOW, NOW)
    with admin_engine.connect() as conn, conn.begin():
        conn.execute(
            sa.text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": MACRO_LOCK_KEYS[MacroProvider.BLS]},
        )
        with pytest.raises(MacroStorageError, match="^concurrent_write$"):
            macro_store.persist(
                identity, MacroProvider.BLS, received((observation(),)), NOW + timedelta(seconds=3)
            )
        macro_store.start_run(MacroProvider.FED, WINDOW, NOW)
    report = macro_store.persist(
        identity, MacroProvider.BLS, received((observation(),)), NOW + timedelta(seconds=3)
    )
    assert report.inserted == 1


@pytest.mark.parametrize(
    "code",
    [MacroFailureCode.HTTP_ERROR, MacroFailureCode.SOURCE_REJECTED, MacroFailureCode.INTERRUPTED],
)
def test_failed_provider_reads_leave_only_controlled_failure_audits(
    macro_store: MacroStore, code: MacroFailureCode
) -> None:
    identity = macro_store.start_run(MacroProvider.BLS, WINDOW, NOW)
    macro_store.fail_run(identity, code, NOW + timedelta(seconds=1))
    assert counts(macro_store.engine) == (0, 0, 0)
    with macro_store.engine.connect() as conn:
        run = conn.execute(sa.select(runs)).mappings().one()
        assert run["status"] == "failed" and run["error_code"] == code.value
        assert run["received_at"] is None and run["received"] == 0
