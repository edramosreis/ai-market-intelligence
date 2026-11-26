"""Store immutable FOMC embedding publications on the isolated vector database."""

# Frozen PostgreSQL DDL preserves the reviewed constraints.
# ruff: noqa: E501
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE_SQL = """
SET LOCAL search_path = pg_catalog, pg_temp;

DO $preflight$
BEGIN
    IF (current_database(), current_user) NOT IN (
        ('market_intelligence_m3', 'm3_admin'),
        ('market_intelligence_test', 'm3_test_admin')
    ) OR current_setting('server_version_num')::integer / 10000 <> 18 THEN
        RAISE EXCEPTION 'FOMC vectors require the isolated M3 PostgreSQL 18 target';
    END IF;
    IF (SELECT version_num FROM public.alembic_version) IS DISTINCT FROM '0008' THEN
        RAISE EXCEPTION 'FOMC vectors require revision 0008';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_available_extension_versions WHERE name = 'vector' AND version = '0.8.7'
    ) OR EXISTS (
        SELECT 1 FROM pg_extension WHERE extname = 'vector'
    ) OR to_regnamespace('vector_support') IS NOT NULL THEN
        RAISE EXCEPTION 'FOMC vectors require the reviewed extension files and a fresh namespace';
    END IF;
END;
$preflight$;

CREATE SCHEMA vector_support;
REVOKE ALL ON SCHEMA vector_support FROM PUBLIC;
CREATE EXTENSION vector WITH SCHEMA vector_support VERSION '0.8.7';
REVOKE ALL ON TYPE vector_support.vector FROM PUBLIC;

-- Match the source text hash as well as the passage ID without rewriting any passage rows.
ALTER TABLE public.fomc_passages
    ADD CONSTRAINT uq_fomc_passage_vector_input UNIQUE (passage_id, text_sha256);

CREATE TABLE public.fomc_embedding_profiles (
    profile_id text PRIMARY KEY CHECK (profile_id ~ '^[0-9a-f]{64}$'),
    config_sha256 text NOT NULL CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
    epoch_id uuid NOT NULL,
    provider text NOT NULL CHECK (provider ~ '^[a-z][a-z0-9_-]{0,39}$'),
    model text NOT NULL CHECK (model ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
    dimensions integer NOT NULL CHECK (dimensions BETWEEN 1 AND 16000),
    tokenizer text NOT NULL CHECK (tokenizer ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$'),
    tokenizer_revision text NOT NULL CHECK (tokenizer_revision ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
    input_recipe text NOT NULL CHECK (input_recipe = 'canonical_passage_utf8_v1'),
    coordinate_format text NOT NULL CHECK (coordinate_format = 'unit_float32_be_v1'),
    distance_metric text NOT NULL CHECK (distance_metric = 'cosine'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(created_at)),
    UNIQUE (config_sha256, epoch_id),
    UNIQUE (profile_id, dimensions)
);

CREATE TABLE public.fomc_embedding_runs (
    run_id uuid PRIMARY KEY,
    profile_id text NOT NULL REFERENCES public.fomc_embedding_profiles (profile_id),
    manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    planned_inputs integer NOT NULL CHECK (planned_inputs BETWEEN 1 AND 2048),
    request_limit integer NOT NULL CHECK (request_limit BETWEEN 1 AND 64),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(started_at)),
    finished_at timestamptz CHECK (isfinite(finished_at) AND finished_at >= started_at),
    state text NOT NULL CHECK (state IN ('running', 'succeeded', 'failed', 'reconciliation_required')),
    error_code text,
    UNIQUE (run_id, profile_id, request_limit),
    CHECK (
        (state = 'running' AND finished_at IS NULL AND error_code IS NULL) OR
        (state = 'succeeded' AND finished_at IS NOT NULL AND error_code IS NULL) OR
        (
            state = 'failed' AND finished_at IS NOT NULL AND error_code IS NOT NULL
            AND error_code IN ('source_changed', 'request_failed', 'cancelled', 'internal_error')
        ) OR
        (
            state = 'reconciliation_required' AND finished_at IS NOT NULL
            AND error_code IS NOT NULL AND error_code = 'dispatch_uncertain'
        )
    )
);
CREATE UNIQUE INDEX uq_fomc_embedding_active_run
    ON public.fomc_embedding_runs (profile_id)
    WHERE state IN ('running', 'reconciliation_required');

CREATE TABLE public.fomc_embedding_requests (
    request_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    profile_id text NOT NULL,
    request_limit integer NOT NULL,
    request_no integer NOT NULL CHECK (request_no BETWEEN 1 AND request_limit),
    input_count integer NOT NULL CHECK (input_count BETWEEN 1 AND 32),
    reserved_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(reserved_at)),
    dispatch_at timestamptz CHECK (isfinite(dispatch_at) AND dispatch_at >= reserved_at),
    finished_at timestamptz CHECK (isfinite(finished_at) AND finished_at >= reserved_at),
    reconciled_at timestamptz CHECK (isfinite(reconciled_at) AND reconciled_at >= finished_at),
    state text NOT NULL CHECK (state IN ('reserved', 'dispatched', 'succeeded', 'failed', 'unresolved')),
    provider_request_id text CHECK (provider_request_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$'),
    returned_model text CHECK (returned_model ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
    prompt_tokens integer CHECK (prompt_tokens BETWEEN 1 AND 300000),
    total_tokens integer CHECK (total_tokens BETWEEN prompt_tokens AND 300000),
    error_code text,
    FOREIGN KEY (run_id, profile_id, request_limit)
        REFERENCES public.fomc_embedding_runs (run_id, profile_id, request_limit),
    UNIQUE (run_id, request_no),
    UNIQUE (request_id, profile_id, input_count),
    UNIQUE (request_id, profile_id, state),
    CHECK ((prompt_tokens IS NULL) = (total_tokens IS NULL)),
    CHECK (finished_at IS NULL OR dispatch_at IS NULL OR finished_at >= dispatch_at),
    CHECK ((reconciled_at IS NOT NULL) = coalesce(error_code = 'reconciled_failure', false)),
    CHECK (
        (state IN ('reserved', 'dispatched') AND (dispatch_at IS NOT NULL) = (state = 'dispatched')
            AND finished_at IS NULL AND error_code IS NULL AND provider_request_id IS NULL
            AND returned_model IS NULL AND total_tokens IS NULL) OR
        (state = 'succeeded' AND dispatch_at IS NOT NULL AND finished_at IS NOT NULL
            AND error_code IS NULL AND provider_request_id IS NOT NULL
            AND returned_model IS NOT NULL AND total_tokens IS NOT NULL) OR
        (state = 'failed' AND finished_at IS NOT NULL AND error_code IS NOT NULL AND (
            (error_code = 'cancelled_before_dispatch' AND dispatch_at IS NULL
                AND provider_request_id IS NULL AND returned_model IS NULL AND total_tokens IS NULL)
            OR (error_code IN ('provider_rejected', 'response_invalid', 'reconciled_failure')
                AND dispatch_at IS NOT NULL)
        )) OR
        (state = 'unresolved' AND dispatch_at IS NOT NULL AND finished_at IS NOT NULL
            AND error_code IS NOT NULL AND error_code = 'dispatch_uncertain')
    )
);

CREATE TABLE public.fomc_embedding_request_items (
    request_id uuid NOT NULL,
    profile_id text NOT NULL,
    input_count integer NOT NULL,
    response_position integer NOT NULL CHECK (response_position >= 0 AND response_position < input_count),
    passage_id text NOT NULL,
    input_sha256 text NOT NULL,
    PRIMARY KEY (request_id, response_position),
    UNIQUE (request_id, passage_id),
    UNIQUE (request_id, response_position, profile_id, passage_id),
    FOREIGN KEY (request_id, profile_id, input_count)
        REFERENCES public.fomc_embedding_requests (request_id, profile_id, input_count),
    FOREIGN KEY (passage_id, input_sha256)
        REFERENCES public.fomc_passages (passage_id, text_sha256)
);

-- A committed receipt is terminal. Reconciliation preserves the first ambiguous outcome time
-- and any receipt facts already known when it was recorded.
CREATE FUNCTION public.fomc_check_embedding_request_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $transition$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.state = 'reserved' THEN
            RETURN NEW;
        END IF;
    ELSIF OLD.state = 'reserved' AND (
        NEW.state = 'dispatched' OR
        (NEW.state = 'failed' AND NEW.error_code = 'cancelled_before_dispatch')
    ) THEN
        IF NEW.state = 'dispatched' AND (
            SELECT count(*) FROM public.fomc_embedding_request_items WHERE request_id = NEW.request_id
        ) <> NEW.input_count THEN
            RAISE EXCEPTION 'Incomplete FOMC embedding reservation' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    ELSIF OLD.state = 'dispatched' AND NEW.dispatch_at = OLD.dispatch_at AND (
        NEW.state IN ('succeeded', 'unresolved') OR
        (NEW.state = 'failed' AND NEW.error_code IN ('provider_rejected', 'response_invalid'))
    ) THEN
        RETURN NEW;
    ELSIF OLD.state = 'unresolved' AND NEW.state = 'failed' AND NEW.error_code = 'reconciled_failure'
        AND NEW.dispatch_at = OLD.dispatch_at AND NEW.finished_at = OLD.finished_at
        AND coalesce(OLD.provider_request_id, NEW.provider_request_id) IS NOT DISTINCT FROM NEW.provider_request_id
        AND coalesce(OLD.returned_model, NEW.returned_model) IS NOT DISTINCT FROM NEW.returned_model
        AND coalesce(OLD.prompt_tokens, NEW.prompt_tokens) IS NOT DISTINCT FROM NEW.prompt_tokens
        AND coalesce(OLD.total_tokens, NEW.total_tokens) IS NOT DISTINCT FROM NEW.total_tokens THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Invalid FOMC embedding request transition' USING ERRCODE = '23514';
END;
$transition$;
REVOKE ALL ON FUNCTION public.fomc_check_embedding_request_transition() FROM PUBLIC;
CREATE TRIGGER ck_fomc_embedding_request_transition
    BEFORE INSERT OR UPDATE ON public.fomc_embedding_requests
    FOR EACH ROW EXECUTE FUNCTION public.fomc_check_embedding_request_transition();

CREATE TABLE public.fomc_passage_embeddings (
    profile_id text NOT NULL,
    passage_id text NOT NULL,
    dimensions integer NOT NULL,
    request_id uuid NOT NULL,
    response_position integer NOT NULL,
    request_state text NOT NULL DEFAULT 'succeeded' CHECK (request_state = 'succeeded'),
    embedding vector_support.vector NOT NULL,
    vector_sha256 text NOT NULL CHECK (vector_sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp() CHECK (isfinite(created_at)),
    PRIMARY KEY (profile_id, passage_id),
    UNIQUE (request_id, response_position),
    FOREIGN KEY (profile_id, dimensions)
        REFERENCES public.fomc_embedding_profiles (profile_id, dimensions),
    FOREIGN KEY (request_id, response_position, profile_id, passage_id)
        REFERENCES public.fomc_embedding_request_items (request_id, response_position, profile_id, passage_id),
    CONSTRAINT fk_fomc_vector_published_request FOREIGN KEY (request_id, profile_id, request_state)
        REFERENCES public.fomc_embedding_requests (request_id, profile_id, state)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (vector_support.vector_dims(embedding) = dimensions),
    CHECK (vector_support.vector_norm(embedding) BETWEEN 0.999 AND 1.001)
);

CREATE FUNCTION public.fomc_check_embedding_publication() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $guard$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.fomc_embedding_requests
        WHERE request_id = NEW.request_id AND state = 'succeeded'
    ) AND (
        (SELECT count(*) FROM public.fomc_embedding_request_items
         WHERE request_id = NEW.request_id) <> NEW.input_count
        OR (SELECT count(*) FROM public.fomc_passage_embeddings
            WHERE request_id = NEW.request_id) <> NEW.input_count
    ) THEN
        RAISE EXCEPTION 'Incomplete FOMC embedding publication' USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$guard$;
REVOKE ALL ON FUNCTION public.fomc_check_embedding_publication() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER ck_fomc_embedding_publication
    AFTER INSERT OR UPDATE ON public.fomc_embedding_requests
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (NEW.state = 'succeeded')
    EXECUTE FUNCTION public.fomc_check_embedding_publication();

-- Equivalent privileges must be restored by idempotent database setup after revision 0009.
DO $access$
DECLARE
    role_prefix text := CASE current_database() WHEN 'market_intelligence_test' THEN 'm3_test' ELSE 'm3' END;
    writer text := role_prefix || '_ingest';
    reader text := role_prefix || '_reader';
    relations constant text :=
        'public.fomc_embedding_profiles, public.fomc_embedding_runs, public.fomc_embedding_requests, ' ||
        'public.fomc_embedding_request_items, public.fomc_passage_embeddings';
BEGIN
    EXECUTE format('REVOKE ALL ON %s FROM PUBLIC, %I, %I', relations, writer, reader);
    EXECUTE format('GRANT USAGE ON SCHEMA vector_support TO %I, %I', writer, reader);
    EXECUTE format('GRANT USAGE ON TYPE vector_support.vector TO %I, %I', writer, reader);
    EXECUTE format('GRANT SELECT ON %s TO %I, %I', relations, writer, reader);
    EXECUTE format('GRANT INSERT ON public.fomc_embedding_runs, public.fomc_embedding_requests, ' ||
                   'public.fomc_embedding_request_items, public.fomc_passage_embeddings TO %I', writer);
    EXECUTE format('GRANT UPDATE (state, finished_at, error_code) ON public.fomc_embedding_runs TO %I', writer);
    EXECUTE format('GRANT UPDATE (state, dispatch_at, finished_at, provider_request_id, returned_model, ' ||
                   'prompt_tokens, total_tokens, error_code, reconciled_at) ' ||
                   'ON public.fomc_embedding_requests TO %I', writer);
END;
$access$;
"""

DOWNGRADE_SQL = """
SET LOCAL search_path = pg_catalog, pg_temp;
DO $guard$
BEGIN
    IF (current_database(), current_user) NOT IN (
        ('market_intelligence_m3', 'm3_admin'),
        ('market_intelligence_test', 'm3_test_admin')
    ) THEN
        RAISE EXCEPTION 'FOMC vectors require the isolated M3 database';
    END IF;
END;
$guard$;
LOCK TABLE public.fomc_passage_embeddings, public.fomc_embedding_request_items,
    public.fomc_embedding_requests, public.fomc_embedding_runs, public.fomc_embedding_profiles
    IN ACCESS EXCLUSIVE MODE;
DO $guard$
BEGIN
    IF EXISTS (SELECT 1 FROM public.fomc_passage_embeddings)
       OR EXISTS (SELECT 1 FROM public.fomc_embedding_request_items)
       OR EXISTS (SELECT 1 FROM public.fomc_embedding_requests)
       OR EXISTS (SELECT 1 FROM public.fomc_embedding_runs)
       OR EXISTS (SELECT 1 FROM public.fomc_embedding_profiles) THEN
        RAISE EXCEPTION 'FOMC vector downgrade requires empty embedding tables';
    END IF;
END;
$guard$;
DROP TABLE public.fomc_passage_embeddings RESTRICT;
DROP TABLE public.fomc_embedding_request_items RESTRICT;
DROP TABLE public.fomc_embedding_requests RESTRICT;
DROP TABLE public.fomc_embedding_runs RESTRICT;
DROP TABLE public.fomc_embedding_profiles RESTRICT;
DROP FUNCTION public.fomc_check_embedding_request_transition() RESTRICT;
DROP FUNCTION public.fomc_check_embedding_publication() RESTRICT;
ALTER TABLE public.fomc_passages DROP CONSTRAINT uq_fomc_passage_vector_input RESTRICT;
DROP EXTENSION vector RESTRICT;
DROP SCHEMA vector_support RESTRICT;
"""


def _apply(ddl: str) -> None:
    if op.get_context().as_sql:
        raise RuntimeError("FOMC vector changes require an online migration")
    bind = op.get_bind()
    search_path = bind.execute(sa.text("SELECT current_setting('search_path')")).scalar_one()
    bind.execute(sa.text(ddl))
    bind.execute(
        sa.text("SELECT pg_catalog.set_config('search_path', :path, true)"), {"path": search_path}
    )


def upgrade() -> None:
    _apply(UPGRADE_SQL)


def downgrade() -> None:
    _apply(DOWNGRADE_SQL)
