"""Create five-minute market candle schema"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('crypto', 'fiat')", name=op.f("ck_assets_kind")),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_assets")),
    )
    op.create_table(
        "data_sources",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_data_sources")),
    )
    op.create_table(
        "markets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("source_product_id", sa.Text(), nullable=False),
        sa.Column("base_asset_code", sa.Text(), nullable=False),
        sa.Column("quote_asset_code", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "base_asset_code <> quote_asset_code", name=op.f("ck_markets_distinct_assets")
        ),
        sa.ForeignKeyConstraint(
            ["base_asset_code"], ["assets.code"], name=op.f("fk_markets_base_asset_code_assets")
        ),
        sa.ForeignKeyConstraint(
            ["quote_asset_code"], ["assets.code"], name=op.f("fk_markets_quote_asset_code_assets")
        ),
        sa.ForeignKeyConstraint(
            ["source_code"], ["data_sources.code"], name=op.f("fk_markets_source_code_data_sources")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_markets")),
        sa.UniqueConstraint("source_code", "source_product_id", name="uq_markets_source_product"),
    )
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("market_id", sa.BigInteger(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("requested_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("received", sa.Integer(), server_default="0", nullable=False),
        sa.Column("inserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unchanged", sa.Integer(), server_default="0", nullable=False),
        sa.Column("missing_buckets", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND finished_at IS NOT NULL "
            "AND finished_at >= started_at)",
            name=op.f("ck_ingestion_runs_lifecycle"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name=op.f("ck_ingestion_runs_status")
        ),
        sa.CheckConstraint(
            "extract(epoch FROM requested_start) % 300 = 0 "
            "AND extract(epoch FROM requested_end) % 300 = 0",
            name=op.f("ck_ingestion_runs_alignment"),
        ),
        sa.CheckConstraint("interval_seconds = 300", name=op.f("ck_ingestion_runs_interval")),
        sa.CheckConstraint(
            "received >= 0 AND inserted >= 0 AND updated >= 0 "
            "AND unchanged >= 0 AND missing_buckets >= 0",
            name=op.f("ck_ingestion_runs_counts"),
        ),
        sa.CheckConstraint(
            "requested_start < requested_end", name=op.f("ck_ingestion_runs_window")
        ),
        sa.ForeignKeyConstraint(
            ["market_id"], ["markets.id"], name=op.f("fk_ingestion_runs_market_id_markets")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_runs")),
        sa.UniqueConstraint(
            "id", "market_id", "interval_seconds", name="uq_ingestion_runs_identity"
        ),
    )
    op.create_table(
        "candles",
        sa.Column("market_id", sa.BigInteger(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("high", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("low", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("close", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("base_volume", sa.Numeric(precision=38, scale=18), nullable=False),
        sa.Column("first_ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ingestion_run_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "\"open\" <> 'NaN'::numeric AND high <> 'NaN'::numeric "
            "AND low <> 'NaN'::numeric AND \"close\" <> 'NaN'::numeric "
            "AND base_volume <> 'NaN'::numeric",
            name=op.f("ck_candles_finite_values"),
        ),
        sa.CheckConstraint(
            '"open" > 0 AND high > 0 AND low > 0 AND "close" > 0 AND base_volume >= 0',
            name=op.f("ck_candles_positive_values"),
        ),
        sa.CheckConstraint(
            "extract(epoch FROM opened_at) % 300 = 0", name=op.f("ck_candles_alignment")
        ),
        sa.CheckConstraint("interval_seconds = 300", name=op.f("ck_candles_interval")),
        sa.CheckConstraint(
            "last_updated_at >= first_ingested_at", name=op.f("ck_candles_provenance_time")
        ),
        sa.CheckConstraint(
            'low <= "open" AND "open" <= high AND low <= "close" AND "close" <= high',
            name=op.f("ck_candles_ohlc"),
        ),
        sa.ForeignKeyConstraint(
            ["last_ingestion_run_id", "market_id", "interval_seconds"],
            ["ingestion_runs.id", "ingestion_runs.market_id", "ingestion_runs.interval_seconds"],
            name="fk_candles_ingestion_run_identity",
        ),
        sa.ForeignKeyConstraint(
            ["market_id"], ["markets.id"], name=op.f("fk_candles_market_id_markets")
        ),
        sa.PrimaryKeyConstraint(
            "market_id", "interval_seconds", "opened_at", name=op.f("pk_candles")
        ),
    )
    # Reference identities belong to the reviewed data contract, not live provider responses.
    op.execute(
        "INSERT INTO data_sources (code, name) VALUES ('coinbase_exchange', 'Coinbase Exchange')"
    )
    op.execute(
        "INSERT INTO assets (code, name, kind) VALUES "
        "('BTC', 'Bitcoin', 'crypto'), ('USD', 'US Dollar', 'fiat')"
    )
    op.execute(
        "INSERT INTO markets (source_code, source_product_id, base_asset_code, quote_asset_code) "
        "VALUES ('coinbase_exchange', 'BTC-USD', 'BTC', 'USD')"
    )


def downgrade() -> None:
    op.drop_table("candles")
    op.drop_table("ingestion_runs")
    op.drop_table("markets")
    op.drop_table("data_sources")
    op.drop_table("assets")
