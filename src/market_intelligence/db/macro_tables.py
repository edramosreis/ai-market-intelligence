"""Monthly macro catalog, receipt audits and immutable locally observed fact versions."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from market_intelligence.db.tables import metadata

macro_series = sa.Table(
    "macro_series",
    metadata,
    sa.Column("source_code", sa.Text, sa.ForeignKey("data_sources.code"), primary_key=True),
    sa.Column("series_id", sa.Text, primary_key=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("unit", sa.Text, nullable=False),
    sa.Column("seasonal_adjustment", sa.Text, nullable=False),
    sa.Column("earliest_month", sa.Date, nullable=False),
    sa.CheckConstraint(
        "(source_code = 'bls' AND series_id = 'CUSR0000SA0' "
        "AND unit = 'index_1982_84_100' AND seasonal_adjustment = 'seasonally_adjusted' "
        "AND earliest_month = DATE '1947-01-01') OR "
        "(source_code = 'bls' AND series_id = 'LNS14000000' AND unit = 'percent' "
        "AND seasonal_adjustment = 'seasonally_adjusted' "
        "AND earliest_month = DATE '1948-01-01') OR "
        "(source_code = 'federal_reserve_board' AND series_id = 'RIFSPFF_N.M' "
        "AND unit = 'percent_per_annum' AND seasonal_adjustment = 'not_seasonally_adjusted' "
        "AND earliest_month = DATE '1954-07-01')",
        name="contract",
    ),
)

macro_ingestion_runs = sa.Table(
    "macro_ingestion_runs",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("source_code", sa.Text, sa.ForeignKey("data_sources.code"), nullable=False),
    sa.Column("requested_start", sa.Date, nullable=False),
    sa.Column("requested_end", sa.Date, nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("fetch_started_at", sa.DateTime(timezone=True)),
    sa.Column("received_at", sa.DateTime(timezone=True)),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("status", sa.Text, nullable=False),
    *[
        sa.Column(name, sa.Integer, nullable=False, server_default="0")
        for name in (
            "received",
            "inserted",
            "corrected",
            "unchanged",
            "source_missing",
            "retained",
            "annual_average_count",
        )
    ],
    sa.Column("prepared_text", sa.Text),
    sa.Column("source_messages", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("latest_hints", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("source_annotations", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("retained_periods", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("error_code", sa.Text),
    sa.UniqueConstraint("id", "source_code", name="uq_macro_runs_identity"),
    sa.CheckConstraint(
        "source_code IN ('bls', 'federal_reserve_board') "
        "AND isfinite(requested_start) AND isfinite(requested_end) "
        "AND requested_start >= DATE '1947-01-01' AND requested_start < requested_end "
        "AND requested_end < DATE '9999-01-01' "
        "AND extract(day FROM requested_start) = 1 AND extract(day FROM requested_end) = 1 "
        "AND requested_end <= (requested_start + interval '100 years')::date "
        "AND (source_code <> 'bls' OR extract(year FROM requested_end - 1) "
        "- extract(year FROM requested_start) < 10)",
        name="window",
    ),
    sa.CheckConstraint(
        "isfinite(started_at) AND ("
        "(status = 'running' AND finished_at IS NULL AND fetch_started_at IS NULL "
        "AND received_at IS NULL AND error_code IS NULL) OR "
        "(status = 'succeeded' AND finished_at IS NOT NULL AND isfinite(finished_at) "
        "AND fetch_started_at IS NOT NULL AND isfinite(fetch_started_at) "
        "AND received_at IS NOT NULL AND isfinite(received_at) "
        "AND started_at <= fetch_started_at AND fetch_started_at <= received_at "
        "AND received_at <= finished_at AND error_code IS NULL) OR "
        "(status = 'failed' AND finished_at IS NOT NULL AND isfinite(finished_at) "
        "AND finished_at >= started_at AND fetch_started_at IS NULL AND received_at IS NULL "
        "AND error_code IS NOT NULL AND error_code IN ('invalid_payload', 'source_rejected', "
        "'http_error', 'retry_exhausted', 'deadline_exceeded', 'database_error', "
        "'interrupted', 'internal_error', 'stale_read')))",
        name="lifecycle",
    ),
    sa.CheckConstraint(
        "received >= 0 AND inserted >= 0 AND corrected >= 0 AND unchanged >= 0 "
        "AND received = inserted + corrected + unchanged AND source_missing BETWEEN 0 AND received "
        "AND retained >= 0 AND received + retained <= "
        "((extract(year FROM requested_end) - extract(year FROM requested_start)) * 12 "
        "+ extract(month FROM requested_end) - extract(month FROM requested_start)) "
        "* (CASE WHEN source_code = 'bls' THEN 2 ELSE 1 END) "
        "AND annual_average_count BETWEEN 0 AND 20 "
        "AND (status = 'succeeded' OR (received = 0 AND retained = 0 "
        "AND annual_average_count = 0))",
        name="counts",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(source_messages) = 'array' AND jsonb_array_length(source_messages) <= 50 "
        "AND jsonb_typeof(latest_hints) = 'array' AND jsonb_array_length(latest_hints) <= 2 "
        "AND jsonb_typeof(source_annotations) = 'array' "
        "AND jsonb_array_length(source_annotations) <= 20 "
        "AND jsonb_typeof(retained_periods) = 'array' "
        "AND jsonb_array_length(retained_periods) = retained "
        "AND (status = 'succeeded' OR (prepared_text IS NULL AND source_messages = '[]'::jsonb "
        "AND latest_hints = '[]'::jsonb AND source_annotations = '[]'::jsonb)) "
        "AND (source_code <> 'bls' OR (prepared_text IS NULL "
        "AND source_annotations = '[]'::jsonb)) "
        "AND (source_code <> 'federal_reserve_board' OR (annual_average_count = 0 "
        "AND source_messages = '[]'::jsonb AND latest_hints = '[]'::jsonb "
        "AND (status <> 'succeeded' OR (prepared_text IS NOT NULL "
        "AND prepared_text ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$'))))",
        name="source_metadata",
    ),
    sa.Index("ix_macro_runs_receipt", "source_code", "received_at", "id"),
)

macro_observed_versions = sa.Table(
    "macro_observed_versions",
    metadata,
    sa.Column("source_code", sa.Text, primary_key=True),
    sa.Column("series_id", sa.Text, primary_key=True),
    sa.Column("observation_month", sa.Date, primary_key=True),
    sa.Column("version_number", sa.Integer, primary_key=True),
    sa.Column("native_period", sa.Text, nullable=False),
    sa.Column("value", sa.Numeric(38, 18)),
    sa.Column("missing_reason", sa.Text),
    sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("ingestion_run_id", UUID(as_uuid=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["source_code", "series_id"],
        ["macro_series.source_code", "macro_series.series_id"],
    ),
    sa.ForeignKeyConstraint(
        ["ingestion_run_id", "source_code"],
        ["macro_ingestion_runs.id", "macro_ingestion_runs.source_code"],
        name="fk_macro_versions_receipt_identity",
    ),
    sa.UniqueConstraint(
        "ingestion_run_id",
        "series_id",
        "observation_month",
        name="uq_macro_version_run_period",
    ),
    sa.CheckConstraint(
        "isfinite(observation_month) AND extract(day FROM observation_month) = 1 "
        "AND observation_month < DATE '9999-01-01' AND version_number > 0 "
        "AND ((series_id = 'CUSR0000SA0' AND observation_month >= DATE '1947-01-01') OR "
        "(series_id = 'LNS14000000' AND observation_month >= DATE '1948-01-01') OR "
        "(series_id = 'RIFSPFF_N.M' AND observation_month >= DATE '1954-07-01'))",
        name="month",
    ),
    sa.CheckConstraint(
        "(source_code = 'bls' AND native_period = to_char(observation_month, 'YYYY-\"M\"MM')) "
        "OR (source_code = 'federal_reserve_board' AND native_period = to_char("
        "observation_month + interval '1 month' - interval '1 day', 'YYYY-MM-DD'))",
        name="native_period",
    ),
    sa.CheckConstraint(
        "(value IS NULL AND missing_reason IS NOT NULL AND missing_reason = 'source_dash' "
        "AND source_code = 'bls') OR "
        "(value IS NOT NULL AND missing_reason IS NULL "
        "AND value NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric) "
        "AND ((series_id = 'CUSR0000SA0' AND value > 0) "
        "OR (series_id = 'LNS14000000' AND value BETWEEN 0 AND 100) "
        "OR (series_id = 'RIFSPFF_N.M' AND value <> -9999)))",
        name="value",
    ),
    sa.CheckConstraint("isfinite(materialized_at)", name="materialization"),
)

macro_version_footnotes = sa.Table(
    "macro_version_footnotes",
    metadata,
    sa.Column("source_code", sa.Text, primary_key=True),
    sa.Column("series_id", sa.Text, primary_key=True),
    sa.Column("observation_month", sa.Date, primary_key=True),
    sa.Column("version_number", sa.Integer, primary_key=True),
    sa.Column("ordinal", sa.Integer, primary_key=True),
    sa.Column("code", sa.Text, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.ForeignKeyConstraint(
        ["source_code", "series_id", "observation_month", "version_number"],
        [
            "macro_observed_versions.source_code",
            "macro_observed_versions.series_id",
            "macro_observed_versions.observation_month",
            "macro_observed_versions.version_number",
        ],
        name="fk_macro_footnotes_version_identity",
    ),
    sa.CheckConstraint(
        "source_code = 'bls' AND ordinal BETWEEN 0 AND 19 "
        "AND char_length(code) BETWEEN 1 AND 32 AND char_length(text) BETWEEN 1 AND 4000",
        name="content",
    ),
)

macro_current = sa.Table(
    "macro_current",
    metadata,
    sa.Column("source_code", sa.Text, primary_key=True),
    sa.Column("series_id", sa.Text, primary_key=True),
    sa.Column("observation_month", sa.Date, primary_key=True),
    sa.Column("version_number", sa.Integer, nullable=False),
    sa.ForeignKeyConstraint(
        ["source_code", "series_id", "observation_month", "version_number"],
        [
            "macro_observed_versions.source_code",
            "macro_observed_versions.series_id",
            "macro_observed_versions.observation_month",
            "macro_observed_versions.version_number",
        ],
        name="fk_macro_current_version_identity",
    ),
)
