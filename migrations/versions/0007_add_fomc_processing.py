"""Store immutable parse generations, passages and completed FOMC attempts."""

# Frozen PostgreSQL DDL retains exact constraint expressions.
# ruff: noqa: E501
from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE public.fomc_parse_generations (
    generation_id text PRIMARY KEY CHECK (generation_id ~ '^[0-9a-f]{64}$'),
    document_id text NOT NULL,
    raw_version_id text NOT NULL,
    raw_sha256 text NOT NULL,
    raw_byte_count integer NOT NULL,
    parser_version text NOT NULL CHECK (parser_version ~ '^[a-z0-9][a-z0-9._-]{0,79}$'),
    parser_config_sha256 text NOT NULL CHECK (parser_config_sha256 ~ '^[0-9a-f]{64}$'),
    parser_config jsonb NOT NULL CHECK (
        jsonb_typeof(parser_config) = 'array' AND octet_length(parser_config::text) <= 8192
    ),
    validator_version text NOT NULL CHECK (char_length(validator_version) BETWEEN 1 AND 64),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    text_sha256 text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    block_count integer NOT NULL CHECK (block_count BETWEEN 1 AND 2000),
    canonical_bytes integer NOT NULL CHECK (canonical_bytes BETWEEN 1 AND 1048576),
    issues jsonb NOT NULL CHECK (
        jsonb_typeof(issues) = 'array' AND octet_length(issues::text) <= 8192
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(created_at)),
    CONSTRAINT uq_fomc_gen_input UNIQUE (raw_version_id, parser_config_sha256),
    CONSTRAINT uq_fomc_gen_audit UNIQUE (generation_id, raw_version_id, parser_config_sha256),
    CONSTRAINT fk_fomc_gen_raw FOREIGN KEY
        (raw_version_id, document_id, raw_sha256, raw_byte_count)
        REFERENCES public.fomc_raw_versions (raw_version_id, document_id, raw_sha256, byte_count)
);

CREATE TABLE public.fomc_parsed_blocks (
    generation_id text NOT NULL REFERENCES public.fomc_parse_generations (generation_id),
    block_ordinal integer NOT NULL CHECK (block_ordinal BETWEEN 0 AND 1999),
    kind text NOT NULL CHECK (
        kind IN ('heading', 'paragraph', 'quote_paragraph', 'list_item', 'footnote', 'auxiliary')
    ),
    body text NOT NULL CHECK (octet_length(body) BETWEEN 1 AND 1048576),
    section_path jsonb NOT NULL CHECK (
        jsonb_typeof(section_path) = 'array' AND octet_length(section_path::text) <= 8192
    ),
    quote_path jsonb NOT NULL CHECK (
        jsonb_typeof(quote_path) = 'array' AND octet_length(quote_path::text) <= 8192
    ),
    list_path jsonb NOT NULL CHECK (
        jsonb_typeof(list_path) = 'array' AND octet_length(list_path::text) <= 8192
    ),
    anchors jsonb NOT NULL CHECK (
        jsonb_typeof(anchors) = 'array' AND octet_length(anchors::text) <= 2097152
    ),
    references_json jsonb NOT NULL CHECK (
        jsonb_typeof(references_json) = 'array' AND octet_length(references_json::text) <= 2097152
    ),
    PRIMARY KEY (generation_id, block_ordinal)
);

CREATE TABLE public.fomc_passage_sets (
    passage_set_id text PRIMARY KEY CHECK (passage_set_id ~ '^[0-9a-f]{64}$'),
    generation_id text NOT NULL REFERENCES public.fomc_parse_generations (generation_id),
    chunker_version text NOT NULL CHECK (chunker_version ~ '^[a-z0-9][a-z0-9._-]{0,79}$'),
    chunker_config_sha256 text NOT NULL CHECK (chunker_config_sha256 ~ '^[0-9a-f]{64}$'),
    chunker_config jsonb NOT NULL CHECK (
        jsonb_typeof(chunker_config) = 'array' AND octet_length(chunker_config::text) <= 8192
    ),
    manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    passage_count integer NOT NULL CHECK (passage_count BETWEEN 1 AND 1024),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(created_at)),
    CONSTRAINT uq_fomc_set_input UNIQUE (generation_id, chunker_config_sha256),
    CONSTRAINT uq_fomc_set_generation UNIQUE (passage_set_id, generation_id),
    CONSTRAINT uq_fomc_set_audit UNIQUE (passage_set_id, generation_id, chunker_config_sha256)
);

CREATE TABLE public.fomc_passages (
    passage_id text PRIMARY KEY CHECK (passage_id ~ '^[0-9a-f]{64}$'),
    passage_set_id text NOT NULL,
    generation_id text NOT NULL,
    passage_ordinal integer NOT NULL CHECK (passage_ordinal BETWEEN 0 AND 1023),
    body text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 2400),
    text_sha256 text NOT NULL CHECK (text_sha256 ~ '^[0-9a-f]{64}$'),
    section_path jsonb NOT NULL CHECK (
        jsonb_typeof(section_path) = 'array' AND octet_length(section_path::text) <= 8192
    ),
    fragmented boolean NOT NULL,
    CONSTRAINT uq_fomc_passage_order UNIQUE (passage_set_id, passage_ordinal),
    CONSTRAINT uq_fomc_passage_span UNIQUE (passage_id, generation_id),
    CONSTRAINT fk_fomc_passage_set FOREIGN KEY (passage_set_id, generation_id)
        REFERENCES public.fomc_passage_sets (passage_set_id, generation_id)
);

CREATE TABLE public.fomc_passage_spans (
    passage_id text NOT NULL,
    generation_id text NOT NULL,
    span_ordinal integer NOT NULL CHECK (span_ordinal BETWEEN 0 AND 1999),
    block_ordinal integer NOT NULL,
    start_offset integer NOT NULL CHECK (start_offset >= 0),
    end_offset integer NOT NULL CHECK (end_offset > start_offset AND end_offset <= 1048576),
    PRIMARY KEY (passage_id, span_ordinal),
    CONSTRAINT fk_fomc_span_passage FOREIGN KEY (passage_id, generation_id)
        REFERENCES public.fomc_passages (passage_id, generation_id),
    CONSTRAINT fk_fomc_span_block FOREIGN KEY (generation_id, block_ordinal)
        REFERENCES public.fomc_parsed_blocks (generation_id, block_ordinal)
);

CREATE TABLE public.fomc_processing_attempts (
    attempt_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    document_id text NOT NULL,
    raw_version_id text NOT NULL,
    raw_sha256 text NOT NULL,
    raw_byte_count integer NOT NULL,
    parser_config_sha256 text NOT NULL CHECK (parser_config_sha256 ~ '^[0-9a-f]{64}$'),
    chunker_config_sha256 text NOT NULL CHECK (chunker_config_sha256 ~ '^[0-9a-f]{64}$'),
    started_at timestamptz NOT NULL CHECK (isfinite(started_at)),
    finished_at timestamptz NOT NULL DEFAULT clock_timestamp()
        CHECK (isfinite(finished_at) AND finished_at >= started_at),
    outcome text NOT NULL,
    error_code text,
    generation_id text,
    passage_set_id text,
    CONSTRAINT uq_fomc_attempt_command_doc UNIQUE (run_id, document_id),
    CONSTRAINT fk_fomc_attempt_raw FOREIGN KEY
        (raw_version_id, document_id, raw_sha256, raw_byte_count)
        REFERENCES public.fomc_raw_versions (raw_version_id, document_id, raw_sha256, byte_count),
    CONSTRAINT fk_fomc_attempt_gen FOREIGN KEY (generation_id, raw_version_id, parser_config_sha256)
        REFERENCES public.fomc_parse_generations
        (generation_id, raw_version_id, parser_config_sha256),
    CONSTRAINT fk_fomc_attempt_set FOREIGN KEY
        (passage_set_id, generation_id, chunker_config_sha256)
        REFERENCES public.fomc_passage_sets
        (passage_set_id, generation_id, chunker_config_sha256),
    CONSTRAINT ck_fomc_attempt_outcome CHECK (
        (
            outcome IN ('created', 'reused')
            AND generation_id IS NOT NULL AND passage_set_id IS NOT NULL
            AND error_code IS NULL
        ) OR (
            outcome = 'stale'
            AND generation_id IS NULL AND passage_set_id IS NULL
            AND error_code IS NOT NULL AND error_code = 'stale_raw_version'
        ) OR (
            outcome = 'failed'
            AND generation_id IS NULL AND passage_set_id IS NULL
            AND error_code IS NOT NULL AND error_code IN (
                'artifact_error', 'identity_mismatch', 'parse_failed', 'chunk_failed',
                'output_conflict', 'database_error', 'internal_error'
            )
        )
    )
);

CREATE INDEX ix_fomc_attempt_status
    ON public.fomc_processing_attempts
    (raw_version_id, parser_config_sha256, chunker_config_sha256, finished_at DESC, attempt_id DESC)
    WHERE outcome <> 'stale';

CREATE UNIQUE INDEX uq_fomc_attempt_created_set
    ON public.fomc_processing_attempts (passage_set_id)
    WHERE outcome = 'created';
"""


def upgrade() -> None:
    op.execute(DDL)


def downgrade() -> None:
    for table in (
        "fomc_processing_attempts",
        "fomc_passage_spans",
        "fomc_passages",
        "fomc_passage_sets",
        "fomc_parsed_blocks",
        "fomc_parse_generations",
    ):
        op.drop_table(table)
