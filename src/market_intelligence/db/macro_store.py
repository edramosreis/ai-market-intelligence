"""Atomic native monthly facts/versions/audits; current pointers never duplicate values."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

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
    utc,
)
from market_intelligence.macro.storage_models import (
    MacroFailureCode,
    MacroStorageError,
    MacroStorageErrorCode,
    MacroWriteReport,
    validate_read,
    validate_window,
)

MACRO_LOCK_KEYS = {MacroProvider.BLS: 194702, MacroProvider.FED: 195401}
Period = tuple[MacroSeries, date]


@dataclass(frozen=True)
class _Stored:
    observation: MonthlyObservation
    version_number: int
    materialized_at: datetime


def _content(row: MonthlyObservation) -> tuple[object, ...]:
    # Footnote ordering is preserved in each version, but alone is not a correction.
    return (
        row.native_period,
        row.value,
        row.missing_reason,
        tuple(sorted((note.code, note.text) for note in row.footnotes)),
    )


class MacroStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def _transaction(self, provider: MacroProvider | None = None) -> Iterator[Connection]:
        try:
            with self.engine.begin() as conn:
                if (
                    provider is not None
                    and not conn.execute(
                        sa.text("SELECT pg_try_advisory_xact_lock(:key)"),
                        {"key": MACRO_LOCK_KEYS[provider]},
                    ).scalar_one()
                ):
                    raise MacroStorageError(MacroStorageErrorCode.CONCURRENT_WRITE)
                yield conn
        except SQLAlchemyError:
            raise MacroStorageError(MacroStorageErrorCode.DATABASE_ERROR) from None

    def start_run(
        self, provider: MacroProvider, window: MonthlyWindow, started_at: datetime
    ) -> UUID:
        validate_window(provider, window)
        started_at = utc(started_at)
        identity = uuid4()
        with self._transaction(provider) as conn:
            conn.execute(
                runs.insert().values(
                    id=identity,
                    source_code=provider.value,
                    requested_start=window.start,
                    requested_end=window.end,
                    started_at=started_at,
                    status="running",
                )
            )
        return identity

    def fail_run(self, run_id: UUID, code: MacroFailureCode, finished_at: datetime) -> None:
        if not isinstance(code, MacroFailureCode):
            raise ValueError("Expected a controlled failure code")
        finished_at = utc(finished_at)
        with self._transaction() as conn:
            run = (
                conn.execute(
                    sa.select(runs)
                    .where(
                        runs.c.id == run_id,
                        runs.c.status == "running",
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if run is None or finished_at < run["started_at"]:
                raise MacroStorageError(MacroStorageErrorCode.INVALID_RUN)
            conn.execute(
                runs.update()
                .where(runs.c.id == run_id)
                .values(
                    status="failed",
                    finished_at=finished_at,
                    error_code=code.value,
                )
            )

    def _existing(
        self,
        conn: Connection,
        provider: MacroProvider,
        window: MonthlyWindow,
    ) -> dict[Period, _Stored]:
        predicate = sa.and_(
            current.c.source_code == provider.value,
            current.c.observation_month >= window.start,
            current.c.observation_month < window.end,
        )
        footnotes: dict[Period, list[Footnote]] = {}
        for note in conn.execute(
            sa.select(notes)
            .select_from(current.join(versions).join(notes))
            .where(predicate)
            .order_by(notes.c.series_id, notes.c.observation_month, notes.c.ordinal)
        ).mappings():
            key = MacroSeries(note["series_id"]), note["observation_month"]
            footnotes.setdefault(key, []).append(Footnote(note["code"], note["text"]))
        result: dict[Period, _Stored] = {}
        for row in conn.execute(
            sa.select(versions).select_from(current.join(versions)).where(predicate)
        ).mappings():
            key = MacroSeries(row["series_id"]), row["observation_month"]
            result[key] = _Stored(
                MonthlyObservation(
                    key[0],
                    key[1],
                    row["native_period"],
                    row["value"],
                    row["missing_reason"],
                    tuple(footnotes.get(key, [])),
                ),
                row["version_number"],
                row["materialized_at"],
            )
        return result

    def persist(
        self,
        run_id: UUID,
        provider: MacroProvider,
        read: ProviderRead,
        finished_at: datetime,
    ) -> MacroWriteReport:
        validate_read(provider, read)
        finished_at = utc(finished_at)
        with self._transaction(provider) as conn:
            run = (
                conn.execute(
                    sa.select(runs)
                    .where(
                        runs.c.id == run_id,
                        runs.c.source_code == provider.value,
                        runs.c.requested_start == read.window.start,
                        runs.c.requested_end == read.window.end,
                        runs.c.status == "running",
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                run is None
                or not run["started_at"] <= read.fetch_started_at <= read.received_at <= finished_at
            ):
                raise MacroStorageError(MacroStorageErrorCode.INVALID_RUN)
            newer = conn.execute(
                sa.select(runs.c.id)
                .where(
                    runs.c.source_code == provider.value,
                    runs.c.status == "succeeded",
                    runs.c.requested_start < read.window.end,
                    runs.c.requested_end > read.window.start,
                    runs.c.received_at > read.received_at,
                )
                .limit(1)
            ).first()
            if newer is not None:
                raise MacroStorageError(MacroStorageErrorCode.STALE_READ)
            existing = self._existing(conn, provider, read.window)
            if any(row.materialized_at > finished_at for row in existing.values()):
                raise MacroStorageError(MacroStorageErrorCode.INVALID_RUN)
            inserted = corrected = unchanged = 0
            changes: list[dict[str, object]] = []
            footnotes: list[dict[str, object]] = []
            pointers: list[dict[str, object]] = []
            for observation in read.observations:
                key = observation.series, observation.month
                previous = existing.get(key)
                if previous is not None and _content(previous.observation) == _content(observation):
                    unchanged += 1
                    continue
                number = 1 if previous is None else previous.version_number + 1
                inserted += previous is None
                corrected += previous is not None
                identity: dict[str, object] = dict(
                    source_code=provider.value,
                    series_id=observation.series.value,
                    observation_month=observation.month,
                    version_number=number,
                )
                changes.append(
                    identity
                    | dict(
                        native_period=observation.native_period,
                        value=observation.value,
                        missing_reason=observation.missing_reason,
                        materialized_at=finished_at,
                        ingestion_run_id=run_id,
                    )
                )
                pointers.append(identity)
                footnotes.extend(
                    identity | dict(ordinal=index, code=note.code, text=note.text)
                    for index, note in enumerate(observation.footnotes)
                )
            if changes:
                conn.execute(versions.insert(), changes)
                if footnotes:
                    conn.execute(notes.insert(), footnotes)
                statement = insert(current)
                conn.execute(
                    statement.on_conflict_do_update(
                        index_elements=[
                            current.c.source_code,
                            current.c.series_id,
                            current.c.observation_month,
                        ],
                        set_={"version_number": statement.excluded.version_number},
                    ),
                    pointers,
                )
            returned = {(row.series, row.month) for row in read.observations}
            retained = tuple(sorted(existing.keys() - returned))
            report = MacroWriteReport(
                run_id,
                provider,
                read.window,
                len(returned),
                inserted,
                corrected,
                unchanged,
                sum(row.value is None for row in read.observations),
                retained,
                read.annual_average_count,
            )
            conn.execute(
                runs.update()
                .where(runs.c.id == run_id)
                .values(
                    status="succeeded",
                    fetch_started_at=read.fetch_started_at,
                    received_at=read.received_at,
                    finished_at=finished_at,
                    received=report.received,
                    inserted=inserted,
                    corrected=corrected,
                    unchanged=unchanged,
                    source_missing=report.source_missing,
                    retained=report.retained,
                    annual_average_count=read.annual_average_count,
                    prepared_text=read.prepared_text,
                    source_messages=list(read.source_messages),
                    latest_hints=[
                        {"series_id": series.value, "month": month.isoformat()}
                        for series, month in read.latest_hints
                    ],
                    source_annotations=[list(pair) for pair in read.source_annotations],
                    retained_periods=[
                        {"series_id": series.value, "month": month.isoformat()}
                        for series, month in retained
                    ],
                )
            )
            return report
