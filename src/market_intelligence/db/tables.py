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

# Treasury observations have native source dates, not UTC candle buckets.
treasury_ingestion_runs = sa.Table(
    "treasury_ingestion_runs",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("source_code", sa.Text, sa.ForeignKey("data_sources.code"), nullable=False),
    sa.Column("dataset_code", sa.Text, nullable=False),
    sa.Column("requested_start", sa.Date, nullable=False),
    sa.Column("requested_end", sa.Date, nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("status", sa.Text, nullable=False),
    *[
        sa.Column(name, sa.Integer, nullable=False, server_default="0")
        for name in (
            "received_dates",
            "received_rates",
            "inserted",
            "updated",
            "unchanged",
            "source_null",
            "field_absent",
            "retained_dates",
        )
    ],
    sa.Column("error_code", sa.Text),
    sa.UniqueConstraint("id", "source_code", "dataset_code", name="uq_treasury_runs_identity"),
    sa.CheckConstraint(
        "source_code = 'us_treasury' AND dataset_code = 'daily_nominal_par_yield_curve'",
        name="dataset",
    ),
    sa.CheckConstraint(
        "isfinite(requested_start) AND isfinite(requested_end) "
        "AND requested_start >= DATE '1990-01-01' "
        "AND requested_start < DATE '9999-01-01' "
        "AND extract(day FROM requested_start) = 1 "
        "AND requested_end = (requested_start + interval '1 month')::date",
        name="window",
    ),
    sa.CheckConstraint(
        "isfinite(started_at) AND ((status = 'running' AND finished_at IS NULL "
        "AND error_code IS NULL) OR (status = 'succeeded' AND isfinite(finished_at) "
        "AND finished_at >= started_at AND error_code IS NULL) OR "
        "(status = 'failed' AND isfinite(finished_at) AND finished_at >= started_at "
        "AND error_code IN ('invalid_payload', 'http_error', 'retry_exhausted', "
        "'deadline_exceeded', 'database_error', 'interrupted', 'internal_error')))",
        name="lifecycle",
    ),
    # Explicit non-null finish/error checks: SQL CHECK treats UNKNOWN as passing.
    sa.CheckConstraint(
        "status = 'running' OR (finished_at IS NOT NULL AND "
        "(status = 'succeeded' OR (status = 'failed' AND error_code IS NOT NULL)))",
        name="completion",
    ),
    sa.CheckConstraint(
        "received_dates BETWEEN 0 AND 31 AND received_rates = received_dates * 14 "
        "AND inserted >= 0 AND updated >= 0 AND unchanged >= 0 "
        "AND received_rates = inserted + updated + unchanged "
        "AND source_null >= 0 AND field_absent >= 0 "
        "AND source_null + field_absent <= received_rates AND retained_dates BETWEEN 0 AND 31 "
        "AND (status = 'succeeded' OR (received_rates = 0 AND retained_dates = 0))",
        name="counts",
    ),
)

treasury_yields = sa.Table(
    "treasury_yields",
    metadata,
    sa.Column("source_code", sa.Text, primary_key=True),
    sa.Column("dataset_code", sa.Text, primary_key=True),
    sa.Column("observed_on", sa.Date, primary_key=True),
    sa.Column("tenor", sa.Text, primary_key=True),
    sa.Column("yield_percent", sa.Numeric(38, 18)),
    sa.Column("missing_reason", sa.Text),
    sa.Column("first_ingested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_ingestion_run_id", UUID(as_uuid=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["last_ingestion_run_id", "source_code", "dataset_code"],
        [
            "treasury_ingestion_runs.id",
            "treasury_ingestion_runs.source_code",
            "treasury_ingestion_runs.dataset_code",
        ],
        name="fk_treasury_yields_run_identity",
    ),
    sa.CheckConstraint(
        "source_code = 'us_treasury' AND dataset_code = 'daily_nominal_par_yield_curve'",
        name="dataset",
    ),
    sa.CheckConstraint(
        "isfinite(observed_on) AND observed_on >= DATE '1990-01-01' "
        "AND observed_on < DATE '9999-01-01'",
        name="date",
    ),
    sa.CheckConstraint(
        "tenor IN ('1M', '1.5M', '2M', '3M', '4M', '6M', '1Y', '2Y', '3Y', "
        "'5Y', '7Y', '10Y', '20Y', '30Y')",
        name="tenor",
    ),
    sa.CheckConstraint(
        "(yield_percent IS NULL AND missing_reason IS NOT NULL "
        "AND missing_reason IN ('source_null', 'field_absent')) OR "
        "(yield_percent IS NOT NULL AND missing_reason IS NULL "
        "AND yield_percent >= 0 AND yield_percent <> 'NaN'::numeric)",
        name="value",
    ),
    sa.CheckConstraint(
        "isfinite(first_ingested_at) AND isfinite(last_updated_at) "
        "AND last_updated_at >= first_ingested_at",
        name="provenance",
    ),
)

perpetual_instruments = sa.Table(
    "perpetual_instruments",
    metadata,
    sa.Column("source_code", sa.Text, sa.ForeignKey("data_sources.code"), primary_key=True),
    sa.Column("code", sa.Text, primary_key=True),
    sa.Column("source_coin", sa.Text, nullable=False),
    sa.Column("base_asset", sa.Text, nullable=False),
    sa.Column("denomination_asset", sa.Text, nullable=False),
    sa.Column("collateral_asset", sa.Text, nullable=False),
    sa.Column("settlement_asset", sa.Text, nullable=False),
    sa.Column("instrument_type", sa.Text, nullable=False),
    sa.Column("contract_size_base", sa.Numeric(38, 18), nullable=False),
    sa.CheckConstraint(
        "source_code = 'hyperliquid' AND code = 'BTC-PERP' AND source_coin = 'BTC' "
        "AND base_asset = 'BTC' AND denomination_asset = 'USDT' AND collateral_asset = 'USDC' "
        "AND settlement_asset = 'USDC' AND instrument_type = 'linear_perpetual' "
        "AND contract_size_base = 1",
        name="contract",
    ),
)

funding_ingestion_runs = sa.Table(
    "funding_ingestion_runs",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("source_code", sa.Text, nullable=False),
    sa.Column("instrument_code", sa.Text, nullable=False),
    sa.Column("requested_start", sa.DateTime(timezone=True), nullable=False),
    sa.Column("requested_end", sa.DateTime(timezone=True), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("status", sa.Text, nullable=False),
    *[
        sa.Column(name, sa.Integer, nullable=False, server_default="0")
        for name in ("expected_hours", "received", "inserted", "updated", "unchanged", "retained")
    ],
    sa.Column("error_code", sa.Text),
    sa.ForeignKeyConstraint(
        ["source_code", "instrument_code"],
        ["perpetual_instruments.source_code", "perpetual_instruments.code"],
    ),
    sa.UniqueConstraint("id", "source_code", "instrument_code", name="uq_funding_runs_identity"),
    sa.CheckConstraint(
        "isfinite(requested_start) AND isfinite(requested_end) "
        "AND requested_start >= TIMESTAMPTZ '2024-01-01 00:00:00+00' "
        "AND requested_start < requested_end "
        "AND requested_end <= requested_start + interval '31 days' "
        "AND mod(extract(epoch FROM requested_start), 3600) = 0 "
        "AND mod(extract(epoch FROM requested_end) * 1000, 1) = 0",
        name="window",
    ),
    sa.CheckConstraint(
        "isfinite(started_at) AND ((status = 'running' "
        "AND finished_at IS NULL AND error_code IS NULL) "
        "OR (status = 'succeeded' AND finished_at IS NOT NULL AND isfinite(finished_at) "
        "AND finished_at >= started_at AND error_code IS NULL) "
        "OR (status = 'failed' AND finished_at IS NOT NULL AND isfinite(finished_at) "
        "AND finished_at >= started_at AND error_code IS NOT NULL AND error_code IN "
        "('invalid_payload','http_error','retry_exhausted','deadline_exceeded',"
        "'database_error','interrupted','internal_error')))",
        name="lifecycle",
    ),
    sa.CheckConstraint(
        "expected_hours BETWEEN 1 AND 745 AND expected_hours = "
        "ceil(extract(epoch FROM (requested_end - requested_start)) / 3600) "
        "AND received >= 0 AND inserted >= 0 AND updated >= 0 AND unchanged >= 0 AND retained >= 0 "
        "AND received = inserted + updated + unchanged AND received + retained <= expected_hours "
        "AND (status = 'succeeded' OR (received = 0 AND retained = 0))",
        name="counts",
    ),
)

funding_events = sa.Table(
    "funding_events",
    metadata,
    sa.Column("source_code", sa.Text, primary_key=True),
    sa.Column("instrument_code", sa.Text, primary_key=True),
    sa.Column("event_at", sa.DateTime(timezone=True), primary_key=True),
    sa.Column("settlement_hour", sa.DateTime(timezone=True), nullable=False),
    sa.Column("funding_rate", sa.Numeric(38, 18), nullable=False),
    sa.Column("premium", sa.Numeric(38, 18), nullable=False),
    sa.Column("first_ingested_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_ingestion_run_id", UUID(as_uuid=True), nullable=False),
    sa.ForeignKeyConstraint(
        ["last_ingestion_run_id", "source_code", "instrument_code"],
        [
            "funding_ingestion_runs.id",
            "funding_ingestion_runs.source_code",
            "funding_ingestion_runs.instrument_code",
        ],
        name="fk_funding_events_run_identity",
    ),
    sa.UniqueConstraint(
        "source_code", "instrument_code", "settlement_hour", name="uq_funding_hour"
    ),
    sa.CheckConstraint(
        "isfinite(event_at) AND event_at >= TIMESTAMPTZ '2024-01-01 00:00:00+00' "
        "AND mod(extract(epoch FROM event_at) * 1000, 1) = 0 AND settlement_hour = "
        "(date_trunc('hour', event_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')",
        name="event_time",
    ),
    sa.CheckConstraint(
        "funding_rate NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric) "
        "AND premium NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
        name="values",
    ),
    sa.CheckConstraint(
        "isfinite(first_ingested_at) AND isfinite(last_updated_at) "
        "AND last_updated_at >= first_ingested_at",
        name="provenance",
    ),
)

open_interest_runs = sa.Table(
    "open_interest_runs",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("source_code", sa.Text, nullable=False),
    sa.Column("instrument_code", sa.Text, nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("received", sa.Integer, nullable=False, server_default="0"),
    sa.Column("error_code", sa.Text),
    sa.ForeignKeyConstraint(
        ["source_code", "instrument_code"],
        ["perpetual_instruments.source_code", "perpetual_instruments.code"],
    ),
    sa.UniqueConstraint("id", "source_code", "instrument_code", name="uq_oi_runs_identity"),
    sa.CheckConstraint(
        "isfinite(started_at) AND ((status = 'running' "
        "AND finished_at IS NULL AND error_code IS NULL "
        "AND received = 0) OR (status = 'succeeded' "
        "AND finished_at IS NOT NULL AND isfinite(finished_at) "
        "AND finished_at >= started_at AND error_code IS NULL AND received = 1) "
        "OR (status = 'failed' AND finished_at IS NOT NULL AND isfinite(finished_at) "
        "AND finished_at >= started_at AND error_code IS NOT NULL "
        "AND received = 0 AND error_code IN "
        "('invalid_payload','http_error','retry_exhausted','deadline_exceeded',"
        "'database_error','interrupted','internal_error')))",
        name="lifecycle",
    ),
)

open_interest_snapshots = sa.Table(
    "open_interest_snapshots",
    metadata,
    sa.Column("snapshot_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("source_code", sa.Text, nullable=False),
    sa.Column("instrument_code", sa.Text, nullable=False),
    sa.Column("fetch_started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("open_interest_btc", sa.Numeric(38, 18), nullable=False),
    sa.Column("mark_price_usdt", sa.Numeric(38, 18), nullable=False),
    sa.Column("oracle_price_usdt", sa.Numeric(38, 18), nullable=False),
    sa.Column("ingestion_run_id", UUID(as_uuid=True), nullable=False, unique=True),
    sa.ForeignKeyConstraint(
        ["ingestion_run_id", "source_code", "instrument_code"],
        [
            "open_interest_runs.id",
            "open_interest_runs.source_code",
            "open_interest_runs.instrument_code",
        ],
        name="fk_oi_snapshots_run_identity",
    ),
    sa.CheckConstraint(
        "isfinite(fetch_started_at) AND isfinite(received_at) AND received_at >= fetch_started_at",
        name="receipt_time",
    ),
    sa.CheckConstraint(
        "open_interest_btc >= 0 AND open_interest_btc NOT IN ('NaN'::numeric, 'Infinity'::numeric) "
        "AND mark_price_usdt > 0 AND mark_price_usdt NOT IN ('NaN'::numeric, 'Infinity'::numeric) "
        "AND oracle_price_usdt > 0 "
        "AND oracle_price_usdt NOT IN ('NaN'::numeric, 'Infinity'::numeric)",
        name="values",
    ),
    sa.Index("ix_oi_receipt", "source_code", "instrument_code", "received_at", "snapshot_id"),
)
