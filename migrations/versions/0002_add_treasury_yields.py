"""Add daily nominal Treasury yields with monthly ingestion provenance.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("INSERT INTO data_sources (code, name) VALUES ('us_treasury', 'US Treasury')")
    op.create_table(
        "treasury_ingestion_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("dataset_code", sa.Text(), nullable=False),
        sa.Column("requested_start", sa.Date(), nullable=False),
        sa.Column("requested_end", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("received_dates", sa.Integer(), server_default="0", nullable=False),
        sa.Column("received_rates", sa.Integer(), server_default="0", nullable=False),
        sa.Column("inserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unchanged", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source_null", sa.Integer(), server_default="0", nullable=False),
        sa.Column("field_absent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retained_dates", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "isfinite(requested_start) AND isfinite(requested_end) AND requested_start >= "
            "DATE '1990-01-01' AND requested_start < DATE '9999-01-01' AND extract(day FROM "
            "requested_start) = 1 AND requested_end = (requested_start + interval '1 "
            "month')::date",
            name=op.f("ck_treasury_ingestion_runs_window"),
        ),
        sa.CheckConstraint(
            "isfinite(started_at) AND ((status = 'running' AND finished_at IS NULL AND "
            "error_code IS NULL) OR (status = 'succeeded' AND isfinite(finished_at) AND "
            "finished_at >= started_at AND error_code IS NULL) OR (status = 'failed' AND "
            "isfinite(finished_at) AND finished_at >= started_at AND error_code IN "
            "('invalid_payload', 'http_error', 'retry_exhausted', 'deadline_exceeded', "
            "'database_error', 'interrupted', 'internal_error')))",
            name=op.f("ck_treasury_ingestion_runs_lifecycle"),
        ),
        sa.CheckConstraint(
            "received_dates BETWEEN 0 AND 31 AND received_rates = received_dates * 14 AND "
            "inserted >= 0 AND updated >= 0 AND unchanged >= 0 AND received_rates = inserted "
            "+ updated + unchanged AND source_null >= 0 AND field_absent >= 0 AND source_null"
            " + field_absent <= received_rates AND retained_dates BETWEEN 0 AND 31 AND "
            "(status = 'succeeded' OR (received_rates = 0 AND retained_dates = 0))",
            name=op.f("ck_treasury_ingestion_runs_counts"),
        ),
        sa.CheckConstraint(
            "source_code = 'us_treasury' AND dataset_code = 'daily_nominal_par_yield_curve'",
            name=op.f("ck_treasury_ingestion_runs_dataset"),
        ),
        sa.CheckConstraint(
            "status = 'running' OR (finished_at IS NOT NULL AND (status = 'succeeded' OR "
            "(status = 'failed' AND error_code IS NOT NULL)))",
            name=op.f("ck_treasury_ingestion_runs_completion"),
        ),
        sa.ForeignKeyConstraint(
            ["source_code"],
            ["data_sources.code"],
            name=op.f("fk_treasury_ingestion_runs_source_code_data_sources"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_treasury_ingestion_runs")),
        sa.UniqueConstraint("id", "source_code", "dataset_code", name="uq_treasury_runs_identity"),
    )
    op.create_table(
        "treasury_yields",
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("dataset_code", sa.Text(), nullable=False),
        sa.Column("observed_on", sa.Date(), nullable=False),
        sa.Column("tenor", sa.Text(), nullable=False),
        sa.Column("yield_percent", sa.Numeric(precision=38, scale=18), nullable=True),
        sa.Column("missing_reason", sa.Text(), nullable=True),
        sa.Column("first_ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ingestion_run_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "(yield_percent IS NULL AND missing_reason IS NOT NULL AND missing_reason IN "
            "('source_null', 'field_absent')) OR (yield_percent IS NOT NULL AND "
            "missing_reason IS NULL AND yield_percent >= 0 AND yield_percent <> "
            "'NaN'::numeric)",
            name=op.f("ck_treasury_yields_value"),
        ),
        sa.CheckConstraint(
            "isfinite(observed_on) AND observed_on >= DATE '1990-01-01' AND observed_on < "
            "DATE '9999-01-01'",
            name=op.f("ck_treasury_yields_date"),
        ),
        sa.CheckConstraint(
            "source_code = 'us_treasury' AND dataset_code = 'daily_nominal_par_yield_curve'",
            name=op.f("ck_treasury_yields_dataset"),
        ),
        sa.CheckConstraint(
            "tenor IN ('1M', '1.5M', '2M', '3M', '4M', '6M', '1Y', '2Y', '3Y', '5Y', '7Y', "
            "'10Y', '20Y', '30Y')",
            name=op.f("ck_treasury_yields_tenor"),
        ),
        sa.CheckConstraint(
            "isfinite(first_ingested_at) AND isfinite(last_updated_at) AND last_updated_at >="
            " first_ingested_at",
            name=op.f("ck_treasury_yields_provenance"),
        ),
        sa.ForeignKeyConstraint(
            ["last_ingestion_run_id", "source_code", "dataset_code"],
            [
                "treasury_ingestion_runs.id",
                "treasury_ingestion_runs.source_code",
                "treasury_ingestion_runs.dataset_code",
            ],
            name="fk_treasury_yields_run_identity",
        ),
        sa.PrimaryKeyConstraint(
            "source_code", "dataset_code", "observed_on", "tenor", name=op.f("pk_treasury_yields")
        ),
    )


def downgrade() -> None:
    op.drop_table("treasury_yields")
    op.drop_table("treasury_ingestion_runs")
    op.execute("DELETE FROM data_sources WHERE code = 'us_treasury'")
