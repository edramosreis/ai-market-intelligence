"""Add native monthly macro receipts and locally observed versions.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO data_sources VALUES ('bls', 'Bureau of Labor Statistics'), "
        "('federal_reserve_board', 'Federal Reserve Board')"
    )
    op.create_table(
        "macro_series",
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("series_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("seasonal_adjustment", sa.Text(), nullable=False),
        sa.Column("earliest_month", sa.Date(), nullable=False),
        sa.CheckConstraint(
            "(source_code = 'bls' AND series_id = 'CUSR0000SA0' AND unit = "
            "'index_1982_84_100' AND seasonal_adjustment = 'seasonally_adjusted' "
            "AND earliest_month = DATE '1947-01-01') OR (source_code = 'bls' AND "
            "series_id = 'LNS14000000' AND unit = 'percent' AND seasonal_adjustment"
            " = 'seasonally_adjusted' AND earliest_month = DATE '1948-01-01') OR "
            "(source_code = 'federal_reserve_board' AND series_id = 'RIFSPFF_N.M' "
            "AND unit = 'percent_per_annum' AND seasonal_adjustment = "
            "'not_seasonally_adjusted' AND earliest_month = DATE '1954-07-01')",
            name=op.f("ck_macro_series_contract"),
        ),
        sa.ForeignKeyConstraint(
            ["source_code"],
            ["data_sources.code"],
            name=op.f("fk_macro_series_source_code_data_sources"),
        ),
        sa.PrimaryKeyConstraint("source_code", "series_id", name=op.f("pk_macro_series")),
    )
    op.create_table(
        "macro_ingestion_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("requested_start", sa.Date(), nullable=False),
        sa.Column("requested_end", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("received", sa.Integer(), server_default="0", nullable=False),
        sa.Column("inserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("corrected", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unchanged", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source_missing", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retained", sa.Integer(), server_default="0", nullable=False),
        sa.Column("annual_average_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("prepared_text", sa.Text(), nullable=True),
        sa.Column(
            "source_messages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "latest_hints",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "source_annotations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "retained_periods",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "isfinite(started_at) AND ((status = 'running' AND finished_at IS NULL "
            "AND fetch_started_at IS NULL AND received_at IS NULL AND error_code IS"
            " NULL) OR (status = 'succeeded' AND finished_at IS NOT NULL AND "
            "isfinite(finished_at) AND fetch_started_at IS NOT NULL AND "
            "isfinite(fetch_started_at) AND received_at IS NOT NULL AND "
            "isfinite(received_at) AND started_at <= fetch_started_at AND "
            "fetch_started_at <= received_at AND received_at <= finished_at AND "
            "error_code IS NULL) OR (status = 'failed' AND finished_at IS NOT NULL "
            "AND isfinite(finished_at) AND finished_at >= started_at AND "
            "fetch_started_at IS NULL AND received_at IS NULL AND error_code IS NOT"
            " NULL AND error_code IN ('invalid_payload', 'source_rejected', "
            "'http_error', 'retry_exhausted', 'deadline_exceeded', "
            "'database_error', 'interrupted', 'internal_error', 'stale_read')))",
            name=op.f("ck_macro_ingestion_runs_lifecycle"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_messages) = 'array' AND "
            "jsonb_array_length(source_messages) <= 50 AND "
            "jsonb_typeof(latest_hints) = 'array' AND "
            "jsonb_array_length(latest_hints) <= 2 AND "
            "jsonb_typeof(source_annotations) = 'array' AND "
            "jsonb_array_length(source_annotations) <= 20 AND "
            "jsonb_typeof(retained_periods) = 'array' AND "
            "jsonb_array_length(retained_periods) = retained AND (status = "
            "'succeeded' OR (prepared_text IS NULL AND source_messages = "
            "'[]'::jsonb AND latest_hints = '[]'::jsonb AND source_annotations = "
            "'[]'::jsonb)) AND (source_code <> 'bls' OR (prepared_text IS NULL AND "
            "source_annotations = '[]'::jsonb)) AND (source_code <> "
            "'federal_reserve_board' OR (annual_average_count = 0 AND "
            "source_messages = '[]'::jsonb AND latest_hints = '[]'::jsonb AND "
            "(status <> 'succeeded' OR (prepared_text IS NOT NULL AND prepared_text"
            " ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$'))))",
            name=op.f("ck_macro_ingestion_runs_source_metadata"),
        ),
        sa.CheckConstraint(
            "source_code IN ('bls', 'federal_reserve_board') AND "
            "isfinite(requested_start) AND isfinite(requested_end) AND "
            "requested_start >= DATE '1947-01-01' AND requested_start < "
            "requested_end AND requested_end < DATE '9999-01-01' AND extract(day "
            "FROM requested_start) = 1 AND extract(day FROM requested_end) = 1 AND "
            "requested_end <= (requested_start + interval '100 years')::date AND "
            "(source_code <> 'bls' OR extract(year FROM requested_end - 1) - "
            "extract(year FROM requested_start) < 10)",
            name=op.f("ck_macro_ingestion_runs_window"),
        ),
        sa.CheckConstraint(
            "received >= 0 AND inserted >= 0 AND corrected >= 0 AND unchanged >= 0 "
            "AND received = inserted + corrected + unchanged AND source_missing "
            "BETWEEN 0 AND received AND retained >= 0 AND received + retained <= "
            "((extract(year FROM requested_end) - extract(year FROM "
            "requested_start)) * 12 + extract(month FROM requested_end) - "
            "extract(month FROM requested_start)) * (CASE WHEN source_code = 'bls' "
            "THEN 2 ELSE 1 END) AND annual_average_count BETWEEN 0 AND 20 AND "
            "(status = 'succeeded' OR (received = 0 AND retained = 0 AND "
            "annual_average_count = 0))",
            name=op.f("ck_macro_ingestion_runs_counts"),
        ),
        sa.ForeignKeyConstraint(
            ["source_code"],
            ["data_sources.code"],
            name=op.f("fk_macro_ingestion_runs_source_code_data_sources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_macro_ingestion_runs")),
        sa.UniqueConstraint("id", "source_code", name="uq_macro_runs_identity"),
    )
    op.create_index(
        "ix_macro_runs_receipt",
        "macro_ingestion_runs",
        ["source_code", "received_at", "id"],
        unique=False,
    )
    op.create_table(
        "macro_observed_versions",
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("series_id", sa.Text(), nullable=False),
        sa.Column("observation_month", sa.Date(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("native_period", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("missing_reason", sa.Text(), nullable=True),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingestion_run_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "(source_code = 'bls' AND native_period = to_char(observation_month, "
            "'YYYY-\"M\"MM')) OR (source_code = 'federal_reserve_board' AND "
            "native_period = to_char(observation_month + interval '1 month' - "
            "interval '1 day', 'YYYY-MM-DD'))",
            name=op.f("ck_macro_observed_versions_native_period"),
        ),
        sa.CheckConstraint(
            "(value IS NULL AND missing_reason IS NOT NULL AND missing_reason = "
            "'source_dash' AND source_code = 'bls') OR (value IS NOT NULL AND "
            "missing_reason IS NULL AND value NOT IN ('NaN'::numeric, "
            "'Infinity'::numeric, '-Infinity'::numeric) AND ((series_id = "
            "'CUSR0000SA0' AND value > 0) OR (series_id = 'LNS14000000' AND value "
            "BETWEEN 0 AND 100) OR (series_id = 'RIFSPFF_N.M' AND value <> -9999)))",
            name=op.f("ck_macro_observed_versions_value"),
        ),
        sa.CheckConstraint(
            "isfinite(materialized_at)",
            name=op.f("ck_macro_observed_versions_materialization"),
        ),
        sa.CheckConstraint(
            "isfinite(observation_month) AND extract(day FROM observation_month) = "
            "1 AND observation_month < DATE '9999-01-01' AND version_number > 0 AND"
            " ((series_id = 'CUSR0000SA0' AND observation_month >= DATE "
            "'1947-01-01') OR (series_id = 'LNS14000000' AND observation_month >= "
            "DATE '1948-01-01') OR (series_id = 'RIFSPFF_N.M' AND observation_month"
            " >= DATE '1954-07-01'))",
            name=op.f("ck_macro_observed_versions_month"),
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id", "source_code"],
            ["macro_ingestion_runs.id", "macro_ingestion_runs.source_code"],
            name="fk_macro_versions_receipt_identity",
        ),
        sa.ForeignKeyConstraint(
            ["source_code", "series_id"],
            ["macro_series.source_code", "macro_series.series_id"],
            name=op.f("fk_macro_observed_versions_source_code_macro_series"),
        ),
        sa.PrimaryKeyConstraint(
            "source_code",
            "series_id",
            "observation_month",
            "version_number",
            name=op.f("pk_macro_observed_versions"),
        ),
        sa.UniqueConstraint(
            "ingestion_run_id", "series_id", "observation_month", name="uq_macro_version_run_period"
        ),
    )
    op.create_table(
        "macro_version_footnotes",
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("series_id", sa.Text(), nullable=False),
        sa.Column("observation_month", sa.Date(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source_code = 'bls' AND ordinal BETWEEN 0 AND 19 AND char_length(code)"
            " BETWEEN 1 AND 32 AND char_length(text) BETWEEN 1 AND 4000",
            name=op.f("ck_macro_version_footnotes_content"),
        ),
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
        sa.PrimaryKeyConstraint(
            "source_code",
            "series_id",
            "observation_month",
            "version_number",
            "ordinal",
            name=op.f("pk_macro_version_footnotes"),
        ),
    )
    op.create_table(
        "macro_current",
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("series_id", sa.Text(), nullable=False),
        sa.Column("observation_month", sa.Date(), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint(
            "source_code", "series_id", "observation_month", name=op.f("pk_macro_current")
        ),
    )
    catalog = sa.table(
        "macro_series",
        sa.column("source_code", sa.Text),
        sa.column("series_id", sa.Text),
        sa.column("title", sa.Text),
        sa.column("unit", sa.Text),
        sa.column("seasonal_adjustment", sa.Text),
        sa.column("earliest_month", sa.Date),
    )
    op.bulk_insert(
        catalog,
        [
            {
                "source_code": "bls",
                "series_id": "CUSR0000SA0",
                "title": "CPI-U all items, US city average",
                "unit": "index_1982_84_100",
                "seasonal_adjustment": "seasonally_adjusted",
                "earliest_month": date(1947, 1, 1),
            },
            {
                "source_code": "bls",
                "series_id": "LNS14000000",
                "title": "Unemployment rate, civilian population age 16 and over",
                "unit": "percent",
                "seasonal_adjustment": "seasonally_adjusted",
                "earliest_month": date(1948, 1, 1),
            },
            {
                "source_code": "federal_reserve_board",
                "series_id": "RIFSPFF_N.M",
                "title": "Monthly effective federal funds rate",
                "unit": "percent_per_annum",
                "seasonal_adjustment": "not_seasonally_adjusted",
                "earliest_month": date(1954, 7, 1),
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("macro_current")
    op.drop_table("macro_version_footnotes")
    op.drop_table("macro_observed_versions")
    op.drop_index("ix_macro_runs_receipt", table_name="macro_ingestion_runs")
    op.drop_table("macro_ingestion_runs")
    op.drop_table("macro_series")
    op.execute("DELETE FROM data_sources WHERE code IN ('bls', 'federal_reserve_board')")
