"""Reader-role snapshots of native current months and immutable observed versions."""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from market_intelligence.config import ApiSettings
from market_intelligence.db.macro_tables import macro_current as current
from market_intelligence.db.macro_tables import macro_ingestion_runs as runs
from market_intelligence.db.macro_tables import macro_observed_versions as versions
from market_intelligence.db.macro_tables import macro_series as catalog
from market_intelligence.db.macro_tables import macro_version_footnotes as notes
from market_intelligence.ingestion.models import utc_now
from market_intelligence.macro.models import BLS_NOTICE, CATALOG, MacroSeries, utc
from market_intelligence.macro.query_models import (
    MacroCoverage,
    MacroFootnoteEvidence,
    MacroLatest,
    MacroMissingRange,
    MacroObservationEvidence,
    MacroObservationPage,
    MacroProvenance,
    MacroReceiptEvidence,
    MacroSeriesEvidence,
    MacroVersionPage,
    UnknownMacroSeriesError,
    month_at,
    month_number,
    observation_after,
    observation_cursor,
    version_after,
    version_cursor,
)
from market_intelligence.queries.models import QueryValidationError


class MacroQueries:
    def __init__(
        self,
        engine: Engine,
        settings: ApiSettings | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.engine = engine
        self.settings = settings or ApiSettings()
        self.now = now

    @contextmanager
    def snapshot(self) -> Iterator[Connection]:
        with (
            self.engine.connect().execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            ) as conn,
            conn.begin(),
        ):
            yield conn

    def series_id(self, value: str) -> MacroSeries:
        try:
            return MacroSeries(value)
        except ValueError:
            raise UnknownMacroSeriesError from None

    def validate_bounds(self, series: MacroSeries, start: date, end: date) -> datetime:
        retrieved = utc(self.now())
        if (
            type(start) is not date
            or type(end) is not date
            or start.day != 1
            or end.day != 1
            or start < CATALOG[series].earliest_month
            or start >= end
            or end > retrieved.date().replace(day=1)
            or month_number(end) - month_number(start) > self.settings.macro_max_window_months
        ):
            raise QueryValidationError(
                "Use first-of-month bounds within the series native history, "
                "start < end, only completed months and the configured month limit"
            )
        return retrieved

    def validate_limit(self, limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise QueryValidationError("Macro page limit must be between 1 and 100")

    def catalog_rows(self, conn: Connection, retrieved: datetime) -> Sequence[Mapping[str, Any]]:
        join = catalog.outerjoin(
            current,
            sa.and_(
                catalog.c.source_code == current.c.source_code,
                catalog.c.series_id == current.c.series_id,
                current.c.observation_month < retrieved.date().replace(day=1),
            ),
        )
        rows = (
            conn.execute(
                sa.select(
                    catalog,
                    sa.func.count(current.c.observation_month).label("stored_months"),
                    sa.func.min(current.c.observation_month).label("first_stored_month"),
                    sa.func.max(current.c.observation_month).label("last_stored_month"),
                )
                .select_from(join)
                .group_by(*catalog.c)
                .order_by(catalog.c.series_id)
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    def series_evidence(self, row: Mapping[str, Any]) -> MacroSeriesEvidence:
        bls = row["source_code"] == "bls"
        return MacroSeriesEvidence(
            series_id=row["series_id"],
            source_code=row["source_code"],
            source_name="US Bureau of Labor Statistics" if bls else "Federal Reserve Board",
            source_url="https://www.bls.gov/developers/"
            if bls
            else "https://www.federalreserve.gov/releases/h15/",
            title=row["title"],
            unit=row["unit"],
            seasonal_adjustment=row["seasonal_adjustment"],
            earliest_native_month=row["earliest_month"],
            stored_months=row["stored_months"],
            first_stored_month=row["first_stored_month"],
            last_stored_month=row["last_stored_month"],
            source_notice=BLS_NOTICE if bls else None,
        )

    def metadata(
        self, conn: Connection, series: MacroSeries, retrieved: datetime
    ) -> MacroSeriesEvidence:
        row = next(
            (row for row in self.catalog_rows(conn, retrieved) if row["series_id"] == series.value),
            None,
        )
        if row is None:
            raise UnknownMacroSeriesError
        return self.series_evidence(row)

    def list_series(self) -> list[MacroSeriesEvidence]:
        retrieved = utc(self.now())
        with self.snapshot() as conn:
            return [self.series_evidence(row) for row in self.catalog_rows(conn, retrieved)]

    def identity(self, table: Any, series: MacroSeries) -> Any:
        return sa.and_(
            table.c.source_code == CATALOG[series].provider, table.c.series_id == series.value
        )

    def content_select(self, series: MacroSeries, *, current_only: bool = True) -> Any:
        first = versions.alias("initial_version")
        join = versions.join(
            first,
            sa.and_(
                versions.c.source_code == first.c.source_code,
                versions.c.series_id == first.c.series_id,
                versions.c.observation_month == first.c.observation_month,
                first.c.version_number == 1,
            ),
        )
        if current_only:
            join = join.join(
                current,
                sa.and_(
                    versions.c.source_code == current.c.source_code,
                    versions.c.series_id == current.c.series_id,
                    versions.c.observation_month == current.c.observation_month,
                    versions.c.version_number == current.c.version_number,
                ),
            )
        return (
            sa.select(versions, first.c.materialized_at.label("first_materialized_at"))
            .select_from(join)
            .where(self.identity(versions, series))
        )

    def evidence(
        self, conn: Connection, series: MacroSeries, rows: Sequence[Mapping[str, Any]]
    ) -> tuple[list[MacroObservationEvidence], list[MacroReceiptEvidence]]:
        if not rows:
            return [], []
        keys = [(row["observation_month"], row["version_number"]) for row in rows]
        footnotes: dict[tuple[date, int], list[MacroFootnoteEvidence]] = {key: [] for key in keys}
        for note in conn.execute(
            sa.select(notes)
            .where(
                self.identity(notes, series),
                sa.tuple_(notes.c.observation_month, notes.c.version_number).in_(keys),
            )
            .order_by(notes.c.observation_month, notes.c.version_number, notes.c.ordinal)
        ).mappings():
            footnotes[note["observation_month"], note["version_number"]].append(
                MacroFootnoteEvidence(code=note["code"], text=note["text"])
            )
        observations = [
            MacroObservationEvidence(
                month=row["observation_month"],
                native_period=row["native_period"],
                version_number=row["version_number"],
                status="available" if row["value"] is not None else "source_missing",
                value=row["value"],
                missing_reason=row["missing_reason"],
                footnotes=footnotes[row["observation_month"], row["version_number"]],
                provenance=MacroProvenance(
                    first_materialized_at=row["first_materialized_at"],
                    materialized_at=row["materialized_at"],
                    content_receipt_id=row["ingestion_run_id"],
                ),
            )
            for row in rows
        ]
        receipts = [
            MacroReceiptEvidence(
                run_id=row["id"],
                start=row["requested_start"],
                end=row["requested_end"],
                fetch_started_at=row["fetch_started_at"],
                received_at=row["received_at"],
                access_date=row["received_at"].date(),
                finished_at=row["finished_at"],
                received_periods=row["received"],
                retained_periods=row["retained"],
                prepared_text=row["prepared_text"],
                source_annotations=row["source_annotations"],
                source_messages=row["source_messages"],
                latest_hints=[
                    (MacroSeries(hint["series_id"]), date.fromisoformat(hint["month"]))
                    for hint in row["latest_hints"]
                ],
            )
            for row in conn.execute(
                sa.select(runs)
                .where(
                    runs.c.source_code == CATALOG[series].provider,
                    runs.c.id.in_({row["ingestion_run_id"] for row in rows}),
                )
                .order_by(runs.c.received_at, runs.c.id)
            ).mappings()
        ]
        return observations, receipts

    def coverage(self, start: date, end: date, rows: Sequence[Mapping[str, Any]]) -> MacroCoverage:
        expected = month_number(end) - month_number(start)
        actual = {row["observation_month"] for row in rows}
        ranges: list[MacroMissingRange] = []
        for number in range(month_number(start), month_number(end)):
            month = month_at(number)
            if month in actual:
                continue
            if ranges and ranges[-1].end == month:
                previous = ranges.pop()
                ranges.append(
                    MacroMissingRange(
                        start=previous.start, end=month_at(number + 1), months=previous.months + 1
                    )
                )
            else:
                ranges.append(MacroMissingRange(start=month, end=month_at(number + 1), months=1))
        available = sum(row["value"] is not None for row in rows)
        return MacroCoverage(
            status="complete"
            if len(actual) == expected
            else ("incomplete" if actual else "no_data"),
            expected_months=expected,
            stored_months=len(actual),
            available_values=available,
            source_missing_values=len(actual) - available,
            not_stored_months=expected - len(actual),
            first_stored_month=min(actual) if actual else None,
            last_stored_month=max(actual) if actual else None,
            missing_ranges=ranges[:100],
            missing_range_count=len(ranges),
            missing_ranges_truncated=len(ranges) > 100,
        )

    def observations(
        self, series_id: str, start: date, end: date, limit: int = 100, cursor: str | None = None
    ) -> MacroObservationPage:
        series = self.series_id(series_id)
        retrieved = self.validate_bounds(series, start, end)
        self.validate_limit(limit)
        after = observation_after(cursor, series, start, end)
        with self.snapshot() as conn:
            metadata = self.metadata(conn, series, retrieved)
            # At most the configured 1,200 native month keys; content remains paginated.
            summary = (
                conn.execute(
                    sa.select(versions.c.observation_month, versions.c.value)
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
                        self.identity(current, series),
                        current.c.observation_month >= start,
                        current.c.observation_month < end,
                    )
                )
                .mappings()
                .all()
            )
            statement = (
                self.content_select(series)
                .where(versions.c.observation_month >= start, versions.c.observation_month < end)
                .order_by(versions.c.observation_month)
                .limit(limit + 1)
            )
            if after is not None:
                statement = statement.where(versions.c.observation_month > after)
            rows = conn.execute(statement).mappings().all()
            more = len(rows) > limit
            observations, receipts = self.evidence(
                conn, series, [dict(row) for row in rows[:limit]]
            )
            return MacroObservationPage(
                series=metadata,
                retrieved_at=retrieved,
                start=start,
                end=end,
                coverage=self.coverage(start, end, [dict(row) for row in summary]),
                observations=observations,
                receipts=receipts,
                next_cursor=observation_cursor(series, start, end, observations[-1].month)
                if more
                else None,
            )

    def latest(self, series_id: str) -> MacroLatest:
        series = self.series_id(series_id)
        retrieved = utc(self.now())
        latest_month = month_at(month_number(retrieved.date()) - 1)
        with self.snapshot() as conn:
            metadata = self.metadata(conn, series, retrieved)
            rows = (
                conn.execute(
                    self.content_select(series)
                    .where(versions.c.observation_month <= latest_month)
                    .order_by(versions.c.observation_month.desc())
                    .limit(1)
                )
                .mappings()
                .all()
            )
            observations, receipts = self.evidence(conn, series, [dict(row) for row in rows])
            observation = observations[0] if observations else None
            return MacroLatest(
                series=metadata,
                retrieved_at=retrieved,
                status="stored" if observation else "no_data",
                observation=observation,
                receipts=receipts,
                latest_completed_month=latest_month,
                months_behind_latest_completed=month_number(latest_month)
                - month_number(observation.month)
                if observation
                else None,
            )

    def observed_versions(
        self, series_id: str, month: date, limit: int = 100, cursor: str | None = None
    ) -> MacroVersionPage:
        series = self.series_id(series_id)
        if type(month) is not date or month.day != 1 or month.year >= 9999:
            raise QueryValidationError("Use a native monthly DATE as YYYY-MM-01")
        retrieved = self.validate_bounds(series, month, month_at(month_number(month) + 1))
        self.validate_limit(limit)
        after = version_after(cursor, series, month)
        with self.snapshot() as conn:
            metadata = self.metadata(conn, series, retrieved)
            current_number = conn.execute(
                sa.select(current.c.version_number).where(
                    self.identity(current, series), current.c.observation_month == month
                )
            ).scalar_one_or_none()
            count = conn.execute(
                sa.select(sa.func.count())
                .select_from(versions)
                .where(self.identity(versions, series), versions.c.observation_month == month)
            ).scalar_one()
            statement = (
                self.content_select(series, current_only=False)
                .where(versions.c.observation_month == month)
                .order_by(versions.c.version_number)
                .limit(limit + 1)
            )
            if after is not None:
                statement = statement.where(versions.c.version_number > after)
            rows = conn.execute(statement).mappings().all()
            more = len(rows) > limit
            evidence, receipts = self.evidence(conn, series, [dict(row) for row in rows[:limit]])
            return MacroVersionPage(
                series=metadata,
                retrieved_at=retrieved,
                month=month,
                current_version_number=current_number,
                observed_versions=count,
                versions=evidence,
                receipts=receipts,
                next_cursor=version_cursor(series, month, evidence[-1].version_number)
                if more
                else None,
            )
