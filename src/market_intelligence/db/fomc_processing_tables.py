"""Metadata matching the append-only FOMC processing migration."""

# ruff: noqa: E501
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from market_intelligence.db import fomc_tables  # noqa: F401
from market_intelligence.db.tables import metadata

fomc_parse_generations = sa.Table(
    "fomc_parse_generations",
    metadata,
    sa.Column(
        "generation_id",
        sa.Text,
        sa.CheckConstraint("generation_id ~ '^[0-9a-f]{64}$'", name="generation_id"),
        primary_key=True,
    ),
    sa.Column("document_id", sa.Text, nullable=False),
    sa.Column("raw_version_id", sa.Text, nullable=False),
    sa.Column("raw_sha256", sa.Text, nullable=False),
    sa.Column("raw_byte_count", sa.Integer, nullable=False),
    sa.Column(
        "parser_version",
        sa.Text,
        sa.CheckConstraint("parser_version ~ '^[a-z0-9][a-z0-9._-]{0,79}$'", name="parser_version"),
        nullable=False,
    ),
    sa.Column(
        "parser_config_sha256",
        sa.Text,
        sa.CheckConstraint("parser_config_sha256 ~ '^[0-9a-f]{64}$'", name="parser_config_sha256"),
        nullable=False,
    ),
    sa.Column(
        "parser_config",
        JSONB,
        sa.CheckConstraint(
            " jsonb_typeof(parser_config) = 'array' AND octet_length(parser_config::text) <= 8192 ",
            name="parser_config",
        ),
        nullable=False,
    ),
    sa.Column(
        "validator_version",
        sa.Text,
        sa.CheckConstraint(
            "char_length(validator_version) BETWEEN 1 AND 64", name="validator_version"
        ),
        nullable=False,
    ),
    sa.Column(
        "content_sha256",
        sa.Text,
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="content_sha256"),
        nullable=False,
    ),
    sa.Column(
        "text_sha256",
        sa.Text,
        sa.CheckConstraint("text_sha256 ~ '^[0-9a-f]{64}$'", name="text_sha256"),
        nullable=False,
    ),
    sa.Column(
        "block_count",
        sa.Integer,
        sa.CheckConstraint("block_count BETWEEN 1 AND 2000", name="block_count"),
        nullable=False,
    ),
    sa.Column(
        "canonical_bytes",
        sa.Integer,
        sa.CheckConstraint("canonical_bytes BETWEEN 1 AND 1048576", name="canonical_bytes"),
        nullable=False,
    ),
    sa.Column(
        "issues",
        JSONB,
        sa.CheckConstraint(
            " jsonb_typeof(issues) = 'array' AND octet_length(issues::text) <= 8192 ", name="issues"
        ),
        nullable=False,
    ),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        sa.CheckConstraint("isfinite(created_at)", name="created_at"),
        nullable=False,
        server_default=sa.text("clock_timestamp()"),
    ),
    sa.UniqueConstraint("raw_version_id", "parser_config_sha256", name="uq_fomc_gen_input"),
    sa.UniqueConstraint(
        "generation_id", "raw_version_id", "parser_config_sha256", name="uq_fomc_gen_audit"
    ),
    sa.ForeignKeyConstraint(
        ["raw_version_id", "document_id", "raw_sha256", "raw_byte_count"],
        [
            "fomc_raw_versions.raw_version_id",
            "fomc_raw_versions.document_id",
            "fomc_raw_versions.raw_sha256",
            "fomc_raw_versions.byte_count",
        ],
        name="fk_fomc_gen_raw",
    ),
)

fomc_parsed_blocks = sa.Table(
    "fomc_parsed_blocks",
    metadata,
    sa.Column(
        "generation_id",
        sa.Text,
        sa.ForeignKey("fomc_parse_generations.generation_id"),
        nullable=False,
    ),
    sa.Column(
        "block_ordinal",
        sa.Integer,
        sa.CheckConstraint("block_ordinal BETWEEN 0 AND 1999", name="block_ordinal"),
        nullable=False,
    ),
    sa.Column(
        "kind",
        sa.Text,
        sa.CheckConstraint(
            " kind IN ('heading', 'paragraph', 'quote_paragraph', 'list_item', 'footnote', 'auxiliary') ",
            name="kind",
        ),
        nullable=False,
    ),
    sa.Column(
        "body",
        sa.Text,
        sa.CheckConstraint("octet_length(body) BETWEEN 1 AND 1048576", name="body"),
        nullable=False,
    ),
    sa.Column(
        "section_path",
        JSONB,
        sa.CheckConstraint(
            " jsonb_typeof(section_path) = 'array' AND octet_length(section_path::text) <= 8192 ",
            name="section_path",
        ),
        nullable=False,
    ),
    sa.Column(
        "quote_path",
        JSONB,
        sa.CheckConstraint(
            " jsonb_typeof(quote_path) = 'array' AND octet_length(quote_path::text) <= 8192 ",
            name="quote_path",
        ),
        nullable=False,
    ),
    sa.Column(
        "list_path",
        JSONB,
        sa.CheckConstraint(
            " jsonb_typeof(list_path) = 'array' AND octet_length(list_path::text) <= 8192 ",
            name="list_path",
        ),
        nullable=False,
    ),
    sa.Column(
        "anchors",
        JSONB,
        sa.CheckConstraint(
            " jsonb_typeof(anchors) = 'array' AND octet_length(anchors::text) <= 2097152 ",
            name="anchors",
        ),
        nullable=False,
    ),
    sa.Column(
        "references_json",
        JSONB,
        sa.CheckConstraint(
            " jsonb_typeof(references_json) = 'array' AND octet_length(references_json::text) <= 2097152 ",
            name="references_json",
        ),
        nullable=False,
    ),
    sa.PrimaryKeyConstraint("generation_id", "block_ordinal"),
)

fomc_passage_sets = sa.Table(
    "fomc_passage_sets",
    metadata,
    sa.Column(
        "passage_set_id",
        sa.Text,
        sa.CheckConstraint("passage_set_id ~ '^[0-9a-f]{64}$'", name="passage_set_id"),
        primary_key=True,
    ),
    sa.Column(
        "generation_id",
        sa.Text,
        sa.ForeignKey("fomc_parse_generations.generation_id"),
        nullable=False,
    ),
    sa.Column(
        "chunker_version",
        sa.Text,
        sa.CheckConstraint(
            "chunker_version ~ '^[a-z0-9][a-z0-9._-]{0,79}$'", name="chunker_version"
        ),
        nullable=False,
    ),
    sa.Column(
        "chunker_config_sha256",
        sa.Text,
        sa.CheckConstraint(
            "chunker_config_sha256 ~ '^[0-9a-f]{64}$'", name="chunker_config_sha256"
        ),
        nullable=False,
    ),
    sa.Column(
        "chunker_config",
        JSONB,
        sa.CheckConstraint(
            " jsonb_typeof(chunker_config) = 'array' AND octet_length(chunker_config::text) <= 8192 ",
            name="chunker_config",
        ),
        nullable=False,
    ),
    sa.Column(
        "manifest_sha256",
        sa.Text,
        sa.CheckConstraint("manifest_sha256 ~ '^[0-9a-f]{64}$'", name="manifest_sha256"),
        nullable=False,
    ),
    sa.Column(
        "passage_count",
        sa.Integer,
        sa.CheckConstraint("passage_count BETWEEN 1 AND 1024", name="passage_count"),
        nullable=False,
    ),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        sa.CheckConstraint("isfinite(created_at)", name="created_at"),
        nullable=False,
        server_default=sa.text("clock_timestamp()"),
    ),
    sa.UniqueConstraint("generation_id", "chunker_config_sha256", name="uq_fomc_set_input"),
    sa.UniqueConstraint("passage_set_id", "generation_id", name="uq_fomc_set_generation"),
    sa.UniqueConstraint(
        "passage_set_id", "generation_id", "chunker_config_sha256", name="uq_fomc_set_audit"
    ),
)

fomc_passages = sa.Table(
    "fomc_passages",
    metadata,
    sa.Column(
        "passage_id",
        sa.Text,
        sa.CheckConstraint("passage_id ~ '^[0-9a-f]{64}$'", name="passage_id"),
        primary_key=True,
    ),
    sa.Column("passage_set_id", sa.Text, nullable=False),
    sa.Column("generation_id", sa.Text, nullable=False),
    sa.Column(
        "passage_ordinal",
        sa.Integer,
        sa.CheckConstraint("passage_ordinal BETWEEN 0 AND 1023", name="passage_ordinal"),
        nullable=False,
    ),
    sa.Column(
        "body",
        sa.Text,
        sa.CheckConstraint("char_length(body) BETWEEN 1 AND 2400", name="body"),
        nullable=False,
    ),
    sa.Column(
        "text_sha256",
        sa.Text,
        sa.CheckConstraint("text_sha256 ~ '^[0-9a-f]{64}$'", name="text_sha256"),
        nullable=False,
    ),
    sa.Column(
        "section_path",
        JSONB,
        sa.CheckConstraint(
            " jsonb_typeof(section_path) = 'array' AND octet_length(section_path::text) <= 8192 ",
            name="section_path",
        ),
        nullable=False,
    ),
    sa.Column("fragmented", sa.Boolean, nullable=False),
    sa.UniqueConstraint("passage_set_id", "passage_ordinal", name="uq_fomc_passage_order"),
    sa.UniqueConstraint("passage_id", "generation_id", name="uq_fomc_passage_span"),
    sa.ForeignKeyConstraint(
        ["passage_set_id", "generation_id"],
        ["fomc_passage_sets.passage_set_id", "fomc_passage_sets.generation_id"],
        name="fk_fomc_passage_set",
    ),
)

fomc_passage_spans = sa.Table(
    "fomc_passage_spans",
    metadata,
    sa.Column("passage_id", sa.Text, nullable=False),
    sa.Column("generation_id", sa.Text, nullable=False),
    sa.Column(
        "span_ordinal",
        sa.Integer,
        sa.CheckConstraint("span_ordinal BETWEEN 0 AND 1999", name="span_ordinal"),
        nullable=False,
    ),
    sa.Column("block_ordinal", sa.Integer, nullable=False),
    sa.Column(
        "start_offset",
        sa.Integer,
        sa.CheckConstraint("start_offset >= 0", name="start_offset"),
        nullable=False,
    ),
    sa.Column(
        "end_offset",
        sa.Integer,
        sa.CheckConstraint(
            "end_offset > start_offset AND end_offset <= 1048576", name="end_offset"
        ),
        nullable=False,
    ),
    sa.PrimaryKeyConstraint("passage_id", "span_ordinal"),
    sa.ForeignKeyConstraint(
        ["passage_id", "generation_id"],
        ["fomc_passages.passage_id", "fomc_passages.generation_id"],
        name="fk_fomc_span_passage",
    ),
    sa.ForeignKeyConstraint(
        ["generation_id", "block_ordinal"],
        ["fomc_parsed_blocks.generation_id", "fomc_parsed_blocks.block_ordinal"],
        name="fk_fomc_span_block",
    ),
)

fomc_processing_attempts = sa.Table(
    "fomc_processing_attempts",
    metadata,
    sa.Column("attempt_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("run_id", UUID(as_uuid=True), nullable=False),
    sa.Column("document_id", sa.Text, nullable=False),
    sa.Column("raw_version_id", sa.Text, nullable=False),
    sa.Column("raw_sha256", sa.Text, nullable=False),
    sa.Column("raw_byte_count", sa.Integer, nullable=False),
    sa.Column(
        "parser_config_sha256",
        sa.Text,
        sa.CheckConstraint("parser_config_sha256 ~ '^[0-9a-f]{64}$'", name="parser_config_sha256"),
        nullable=False,
    ),
    sa.Column(
        "chunker_config_sha256",
        sa.Text,
        sa.CheckConstraint(
            "chunker_config_sha256 ~ '^[0-9a-f]{64}$'", name="chunker_config_sha256"
        ),
        nullable=False,
    ),
    sa.Column(
        "started_at",
        sa.DateTime(timezone=True),
        sa.CheckConstraint("isfinite(started_at)", name="started_at"),
        nullable=False,
    ),
    sa.Column(
        "finished_at",
        sa.DateTime(timezone=True),
        sa.CheckConstraint(
            "isfinite(finished_at) AND finished_at >= started_at", name="finished_at"
        ),
        nullable=False,
        server_default=sa.text("clock_timestamp()"),
    ),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("error_code", sa.Text),
    sa.Column("generation_id", sa.Text),
    sa.Column("passage_set_id", sa.Text),
    sa.UniqueConstraint("run_id", "document_id", name="uq_fomc_attempt_command_doc"),
    sa.ForeignKeyConstraint(
        ["raw_version_id", "document_id", "raw_sha256", "raw_byte_count"],
        [
            "fomc_raw_versions.raw_version_id",
            "fomc_raw_versions.document_id",
            "fomc_raw_versions.raw_sha256",
            "fomc_raw_versions.byte_count",
        ],
        name="fk_fomc_attempt_raw",
    ),
    sa.ForeignKeyConstraint(
        ["generation_id", "raw_version_id", "parser_config_sha256"],
        [
            "fomc_parse_generations.generation_id",
            "fomc_parse_generations.raw_version_id",
            "fomc_parse_generations.parser_config_sha256",
        ],
        name="fk_fomc_attempt_gen",
    ),
    sa.ForeignKeyConstraint(
        ["passage_set_id", "generation_id", "chunker_config_sha256"],
        [
            "fomc_passage_sets.passage_set_id",
            "fomc_passage_sets.generation_id",
            "fomc_passage_sets.chunker_config_sha256",
        ],
        name="fk_fomc_attempt_set",
    ),
    sa.CheckConstraint(
        " ( outcome IN ('created', 'reused') AND generation_id IS NOT NULL AND passage_set_id IS NOT NULL AND error_code IS NULL ) OR ( outcome = 'stale' AND generation_id IS NULL AND passage_set_id IS NULL AND error_code IS NOT NULL AND error_code = 'stale_raw_version' ) OR ( outcome = 'failed' AND generation_id IS NULL AND passage_set_id IS NULL AND error_code IS NOT NULL AND error_code IN ( 'artifact_error', 'identity_mismatch', 'parse_failed', 'chunk_failed', 'output_conflict', 'database_error', 'internal_error' ) ) ",
        name=sa.schema.conv("ck_fomc_attempt_outcome"),
    ),
)

sa.Index(
    "ix_fomc_attempt_status",
    fomc_processing_attempts.c.raw_version_id,
    fomc_processing_attempts.c.parser_config_sha256,
    fomc_processing_attempts.c.chunker_config_sha256,
    fomc_processing_attempts.c.finished_at.desc(),
    fomc_processing_attempts.c.attempt_id.desc(),
    postgresql_where=sa.text("outcome <> 'stale'"),
)
sa.Index(
    "uq_fomc_attempt_created_set",
    fomc_processing_attempts.c.passage_set_id,
    unique=True,
    postgresql_where=sa.text("outcome = 'created'"),
)

PROCESSING_TABLE_NAMES = (
    fomc_parse_generations.name,
    fomc_parsed_blocks.name,
    fomc_passage_sets.name,
    fomc_passages.name,
    fomc_passage_spans.name,
    fomc_processing_attempts.name,
)
