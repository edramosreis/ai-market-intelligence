"""Add BTC perpetual funding and forward OI facts/audits.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("INSERT INTO data_sources (code, name) VALUES ('hyperliquid', 'Hyperliquid')")
    op.create_table(
        "perpetual_instruments",
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("source_coin", sa.Text(), nullable=False),
        sa.Column("base_asset", sa.Text(), nullable=False),
        sa.Column("denomination_asset", sa.Text(), nullable=False),
        sa.Column("collateral_asset", sa.Text(), nullable=False),
        sa.Column("settlement_asset", sa.Text(), nullable=False),
        sa.Column("instrument_type", sa.Text(), nullable=False),
        sa.Column("contract_size_base", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.CheckConstraint(
            "source_code = 'hyperliquid' AND code = 'BTC-PERP' AND source_coin = 'BTC' AND "
            "base_asset = 'BTC' AND denomination_asset = 'USDT' AND collateral_asset = "
            "'USDC' AND settlement_asset = 'USDC' AND instrument_type = 'linear_perpetual' "
            "AND contract_size_base = 1",
            name=op.f("ck_perpetual_instruments_contract"),
        ),
        sa.ForeignKeyConstraint(
            ["source_code"],
            ["data_sources.code"],
            name=op.f("fk_perpetual_instruments_source_code_data_sources"),
        ),
        sa.PrimaryKeyConstraint("source_code", "code", name=op.f("pk_perpetual_instruments")),
    )
    op.create_table(
        "funding_ingestion_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("instrument_code", sa.Text(), nullable=False),
        sa.Column("requested_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expected_hours", sa.Integer(), server_default="0", nullable=False),
        sa.Column("received", sa.Integer(), server_default="0", nullable=False),
        sa.Column("inserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unchanged", sa.Integer(), server_default="0", nullable=False),
        sa.Column("retained", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "expected_hours BETWEEN 1 AND 745 AND expected_hours = ceil(extract(epoch FROM "
            "(requested_end - requested_start)) / 3600) AND received >= 0 AND inserted >= 0 "
            "AND updated >= 0 AND unchanged >= 0 AND retained >= 0 AND received = inserted + "
            "updated + unchanged AND received + retained <= expected_hours AND (status = "
            "'succeeded' OR (received = 0 AND retained = 0))",
            name=op.f("ck_funding_ingestion_runs_counts"),
        ),
        sa.CheckConstraint(
            "isfinite(requested_start) AND isfinite(requested_end) AND requested_start >= "
            "TIMESTAMPTZ '2024-01-01 00:00:00+00' AND requested_start < requested_end AND "
            "requested_end <= requested_start + interval '31 days' AND mod(extract(epoch "
            "FROM requested_start), 3600) = 0 AND mod(extract(epoch FROM requested_end) * "
            "1000, 1) = 0",
            name=op.f("ck_funding_ingestion_runs_window"),
        ),
        sa.CheckConstraint(
            "isfinite(started_at) AND ((status = 'running' AND finished_at IS NULL AND "
            "error_code IS NULL) OR (status = 'succeeded' AND finished_at IS NOT NULL AND "
            "isfinite(finished_at) AND finished_at >= started_at AND error_code IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND isfinite(finished_at) AND "
            "finished_at >= started_at AND error_code IS NOT NULL AND error_code IN "
            "('invalid_payload','http_error','retry_exhausted','deadline_exceeded','database_"
            "error','interrupted','internal_error')))",
            name=op.f("ck_funding_ingestion_runs_lifecycle"),
        ),
        sa.ForeignKeyConstraint(
            ["source_code", "instrument_code"],
            ["perpetual_instruments.source_code", "perpetual_instruments.code"],
            name=op.f("fk_funding_ingestion_runs_source_code_perpetual_instruments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_funding_ingestion_runs")),
        sa.UniqueConstraint(
            "id", "source_code", "instrument_code", name="uq_funding_runs_identity"
        ),
    )
    op.create_table(
        "funding_events",
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("instrument_code", sa.Text(), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settlement_hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("funding_rate", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("premium", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("first_ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ingestion_run_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "funding_rate NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric) "
            "AND premium NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name=op.f("ck_funding_events_values"),
        ),
        sa.CheckConstraint(
            "isfinite(event_at) AND event_at >= TIMESTAMPTZ '2024-01-01 00:00:00+00' AND "
            "mod(extract(epoch FROM event_at) * 1000, 1) = 0 AND settlement_hour = "
            "(date_trunc('hour', event_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')",
            name=op.f("ck_funding_events_event_time"),
        ),
        sa.CheckConstraint(
            "isfinite(first_ingested_at) AND isfinite(last_updated_at) AND last_updated_at "
            ">= first_ingested_at",
            name=op.f("ck_funding_events_provenance"),
        ),
        sa.ForeignKeyConstraint(
            ["last_ingestion_run_id", "source_code", "instrument_code"],
            [
                "funding_ingestion_runs.id",
                "funding_ingestion_runs.source_code",
                "funding_ingestion_runs.instrument_code",
            ],
            name="fk_funding_events_run_identity",
        ),
        sa.PrimaryKeyConstraint(
            "source_code", "instrument_code", "event_at", name=op.f("pk_funding_events")
        ),
        sa.UniqueConstraint(
            "source_code", "instrument_code", "settlement_hour", name="uq_funding_hour"
        ),
    )
    op.create_table(
        "open_interest_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("instrument_code", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("received", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "isfinite(started_at) AND ((status = 'running' AND finished_at IS NULL AND "
            "error_code IS NULL AND received = 0) OR (status = 'succeeded' AND finished_at "
            "IS NOT NULL AND isfinite(finished_at) AND finished_at >= started_at AND "
            "error_code IS NULL AND received = 1) OR (status = 'failed' AND finished_at IS "
            "NOT NULL AND isfinite(finished_at) AND finished_at >= started_at AND error_code "
            "IS NOT NULL AND received = 0 AND error_code IN "
            "('invalid_payload','http_error','retry_exhausted','deadline_exceeded','database_"
            "error','interrupted','internal_error')))",
            name=op.f("ck_open_interest_runs_lifecycle"),
        ),
        sa.ForeignKeyConstraint(
            ["source_code", "instrument_code"],
            ["perpetual_instruments.source_code", "perpetual_instruments.code"],
            name=op.f("fk_open_interest_runs_source_code_perpetual_instruments"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_open_interest_runs")),
        sa.UniqueConstraint("id", "source_code", "instrument_code", name="uq_oi_runs_identity"),
    )
    op.create_table(
        "open_interest_snapshots",
        sa.Column("snapshot_id", sa.UUID(), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("instrument_code", sa.Text(), nullable=False),
        sa.Column("fetch_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_interest_btc", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("mark_price_usdt", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("oracle_price_usdt", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("ingestion_run_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "open_interest_btc >= 0 AND open_interest_btc NOT IN ('NaN'::numeric, "
            "'Infinity'::numeric) AND mark_price_usdt > 0 AND mark_price_usdt NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric) AND oracle_price_usdt > 0 AND "
            "oracle_price_usdt NOT IN ('NaN'::numeric, 'Infinity'::numeric)",
            name=op.f("ck_open_interest_snapshots_values"),
        ),
        sa.CheckConstraint(
            "isfinite(fetch_started_at) AND isfinite(received_at) AND received_at >= "
            "fetch_started_at",
            name=op.f("ck_open_interest_snapshots_receipt_time"),
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id", "source_code", "instrument_code"],
            [
                "open_interest_runs.id",
                "open_interest_runs.source_code",
                "open_interest_runs.instrument_code",
            ],
            name="fk_oi_snapshots_run_identity",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_open_interest_snapshots")),
        sa.UniqueConstraint(
            "ingestion_run_id", name=op.f("uq_open_interest_snapshots_ingestion_run_id")
        ),
    )
    op.create_index(
        "ix_oi_receipt",
        "open_interest_snapshots",
        ["source_code", "instrument_code", "received_at", "snapshot_id"],
        unique=False,
    )
    op.execute(
        "INSERT INTO perpetual_instruments VALUES ('hyperliquid', 'BTC-PERP', 'BTC', 'BTC', "
        "'USDT', 'USDC', 'USDC', 'linear_perpetual', 1)"
    )


def downgrade() -> None:
    op.drop_table("open_interest_snapshots")
    op.drop_table("open_interest_runs")
    op.drop_table("funding_events")
    op.drop_table("funding_ingestion_runs")
    op.drop_table("perpetual_instruments")
    op.execute("DELETE FROM data_sources WHERE code = 'hyperliquid'")
