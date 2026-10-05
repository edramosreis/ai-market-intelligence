"""Current schema metadata; immutable changes are recorded in Alembic revisions."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

metadata = sa.MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

data_sources = sa.Table(
    "data_sources",
    metadata,
    sa.Column("code", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
)

assets = sa.Table(
    "assets",
    metadata,
    sa.Column("code", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.CheckConstraint("kind IN ('crypto', 'fiat')", name="kind"),
)

markets = sa.Table(
    "markets",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
    sa.Column("source_code", sa.Text, sa.ForeignKey("data_sources.code"), nullable=False),
    sa.Column("source_product_id", sa.Text, nullable=False),
    sa.Column("base_asset_code", sa.Text, sa.ForeignKey("assets.code"), nullable=False),
    sa.Column("quote_asset_code", sa.Text, sa.ForeignKey("assets.code"), nullable=False),
    sa.UniqueConstraint("source_code", "source_product_id", name="uq_markets_source_product"),
    sa.CheckConstraint("base_asset_code <> quote_asset_code", name="distinct_assets"),
)

ingestion_runs = sa.Table(
    "ingestion_runs",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("market_id", sa.BigInteger, sa.ForeignKey("markets.id"), nullable=False),
    sa.Column("interval_seconds", sa.Integer, nullable=False),
    sa.Column("requested_start", sa.DateTime(timezone=True), nullable=False),
    sa.Column("requested_end", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("status", sa.Text, nullable=False),
    *[
        sa.Column(name, sa.Integer, nullable=False, server_default="0")
        for name in ("received", "inserted", "updated", "unchanged", "missing_buckets")
    ],
    sa.Column("error_code", sa.Text),
    sa.CheckConstraint("interval_seconds = 300", name="interval"),
    sa.CheckConstraint("requested_start < requested_end", name="window"),
    sa.CheckConstraint(
        "extract(epoch FROM requested_start) % 300 = 0 "
        "AND extract(epoch FROM requested_end) % 300 = 0",
        name="alignment",
    ),
    sa.CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status"),
    sa.CheckConstraint(
        "(status = 'running' AND finished_at IS NULL) OR "
        "(status IN ('succeeded', 'failed') AND finished_at IS NOT NULL "
        "AND finished_at >= started_at)",
        name="lifecycle",
    ),
    sa.CheckConstraint(
        "received >= 0 AND inserted >= 0 AND updated >= 0 "
        "AND unchanged >= 0 AND missing_buckets >= 0",
        name="counts",
    ),
    sa.UniqueConstraint("id", "market_id", "interval_seconds", name="uq_ingestion_runs_identity"),
)

candles = sa.Table(
    "candles",
    metadata,
    sa.Column("market_id", sa.BigInteger, sa.ForeignKey("markets.id"), primary_key=True),
    sa.Column("interval_seconds", sa.Integer, primary_key=True),
    sa.Column("opened_at", sa.DateTime(timezone=True), primary_key=True),
    *[
        sa.Column(name, sa.Numeric(38, 18), nullable=False)
        for name in ("open", "high", "low", "close", "base_volume")
    ],
    sa.Column("first_ingested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_ingestion_run_id", UUID(as_uuid=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["last_ingestion_run_id", "market_id", "interval_seconds"],
        ["ingestion_runs.id", "ingestion_runs.market_id", "ingestion_runs.interval_seconds"],
        name="fk_candles_ingestion_run_identity",
    ),
    sa.CheckConstraint("interval_seconds = 300", name="interval"),
    sa.CheckConstraint("extract(epoch FROM opened_at) % 300 = 0", name="alignment"),
    sa.CheckConstraint(
        '"open" > 0 AND high > 0 AND low > 0 AND "close" > 0 AND base_volume >= 0',
        name="positive_values",
    ),
    sa.CheckConstraint(
        "\"open\" <> 'NaN'::numeric AND high <> 'NaN'::numeric "
        "AND low <> 'NaN'::numeric AND \"close\" <> 'NaN'::numeric "
        "AND base_volume <> 'NaN'::numeric",
        name="finite_values",
    ),
    sa.CheckConstraint(
        'low <= "open" AND "open" <= high AND low <= "close" AND "close" <= high',
        name="ohlc",
    ),
    sa.CheckConstraint("last_updated_at >= first_ingested_at", name="provenance_time"),
)
