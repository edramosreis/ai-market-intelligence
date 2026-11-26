"""Reserve and publish supplied FOMC embeddings in isolated PostgreSQL transactions."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from market_intelligence.fomc.retrieval import PassageScope, current_policy_passages
from market_intelligence.fomc.vector_db import (
    check_vector_connection,
    load_profile,
    require_vector_target,
)
from market_intelligence.fomc.vector_models import (
    MAX_BATCH_INPUTS,
    MAX_RUN_INPUTS,
    MAX_RUN_REQUESTS,
    EmbeddingError,
    EmbeddingInput,
    EmbeddingReceipt,
    EmbeddingReceiptEvidence,
    manifest_hash,
    sha256,
)

Failure = Literal[
    "cancelled_before_dispatch", "response_invalid", "provider_rejected", "dispatch_uncertain"
]


@dataclass(frozen=True)
class EmbeddingRun:
    run_id: UUID
    profile_id: str
    scope: PassageScope
    inputs: tuple[EmbeddingInput, ...]
    request_limit: int

    @property
    def manifest_sha256(self) -> str:
        return manifest_hash(self.profile_id, self.inputs)


@dataclass(frozen=True)
class EmbeddingBatch:
    request_id: UUID
    inputs: tuple[EmbeddingInput, ...]


def _current_inputs(conn: Connection, scope: PassageScope) -> tuple[EmbeddingInput, ...]:
    _, query = current_policy_passages(conn, scope)
    policy = query.subquery()
    rows = conn.execute(
        sa.select(policy).order_by(policy.c.passage_id.collate("C")).limit(MAX_RUN_INPUTS + 1)
    ).all()
    if len(rows) > MAX_RUN_INPUTS:
        raise EmbeddingError("embedding_scope_too_large")
    if any(sha256(row.body.encode("utf-8")) != row.text_sha256 for row in rows):
        raise EmbeddingError("source_identity_mismatch")
    return tuple(EmbeddingInput(row.passage_id, row.text_sha256) for row in rows)


def _missing(conn: Connection, run: EmbeddingRun) -> tuple[EmbeddingInput, ...]:
    published = set(
        conn.execute(
            sa.text(
                """
                SELECT passage_id FROM public.fomc_passage_embeddings
                WHERE profile_id = :profile AND passage_id = ANY(:passages)
                """
            ),
            {"profile": run.profile_id, "passages": [item.passage_id for item in run.inputs]},
        ).scalars()
    )
    return tuple(item for item in run.inputs if item.passage_id not in published)


def _lock_run(conn: Connection, run: EmbeddingRun) -> None:
    row = conn.execute(
        sa.text(
            """
            SELECT profile_id, manifest_sha256, planned_inputs, request_limit
            FROM public.fomc_embedding_runs WHERE run_id = :run AND state = 'running'
            FOR UPDATE
            """
        ),
        {"run": run.run_id},
    ).one_or_none()
    if row is None or tuple(row) != (
        run.profile_id,
        run.manifest_sha256,
        len(run.inputs),
        run.request_limit,
    ):
        raise EmbeddingError("embedding_run_unavailable")


def _finish_run(
    conn: Connection, run: EmbeddingRun, state: str, error_code: str | None = None
) -> None:
    conn.execute(
        sa.text(
            """
            UPDATE public.fomc_embedding_runs SET state = :state, finished_at = clock_timestamp(),
                error_code = :error
            WHERE run_id = :run
            """
        ),
        {"run": run.run_id, "state": state, "error": error_code},
    )


def _require_request(
    conn: Connection, run: EmbeddingRun, batch: EmbeddingBatch, state: str
) -> None:
    rows = conn.execute(
        sa.text(
            """
            SELECT i.response_position, i.passage_id, i.input_sha256, r.input_count
            FROM public.fomc_embedding_requests AS r
            JOIN public.fomc_embedding_request_items AS i USING (request_id)
            WHERE r.run_id = :run AND r.request_id = :request AND r.state = :state
            ORDER BY i.response_position
            """
        ),
        {"run": run.run_id, "request": batch.request_id, "state": state},
    ).all()
    expected = [
        (index, item.passage_id, item.input_sha256, len(batch.inputs))
        for index, item in enumerate(batch.inputs)
    ]
    if not expected or rows != expected:
        raise EmbeddingError("embedding_request_unavailable")


class FomcEmbeddingStore:
    def __init__(self, engine: Engine) -> None:
        require_vector_target(engine, "ingest")
        self.engine = engine

    @contextmanager
    def _transaction(self) -> Iterator[Connection]:
        try:
            with (
                self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
                conn.begin(),
            ):
                check_vector_connection(conn, self.engine)
                yield conn
        except sa.exc.DBAPIError as error:
            details = getattr(error.orig, "diag", None)
            if getattr(details, "constraint_name", None) == "uq_fomc_embedding_active_run":
                raise EmbeddingError("embedding_run_active") from None
            if getattr(error.orig, "sqlstate", None) == "40001":
                raise EmbeddingError("embedding_state_changed") from None
            raise EmbeddingError("vector_unavailable") from None

    def start_run(
        self, profile_id: str, scope: PassageScope, *, request_limit: int = MAX_RUN_REQUESTS
    ) -> EmbeddingRun | None:
        if type(request_limit) is not int or not 1 <= request_limit <= MAX_RUN_REQUESTS:
            raise EmbeddingError("invalid_request_limit")
        with self._transaction() as conn:
            load_profile(conn, profile_id)
            run = EmbeddingRun(
                uuid4(), profile_id, scope, _current_inputs(conn, scope), request_limit
            )
            if not run.inputs:
                return None
            remaining = _missing(conn, run)
            if not remaining:
                return None
            if len(remaining) > request_limit * MAX_BATCH_INPUTS:
                raise EmbeddingError("insufficient_request_limit")
            conn.execute(
                sa.text(
                    """
                    INSERT INTO public.fomc_embedding_runs
                        (run_id, profile_id, manifest_sha256, planned_inputs, request_limit, state)
                    VALUES (:run, :profile, :manifest, :planned, :request_limit, 'running')
                    """
                ),
                dict(
                    run=run.run_id,
                    profile=profile_id,
                    manifest=run.manifest_sha256,
                    planned=len(run.inputs),
                    request_limit=request_limit,
                ),
            )
            return run

    def reserve_next(self, run: EmbeddingRun) -> EmbeddingBatch | None:
        with self._transaction() as conn:
            _lock_run(conn, run)
            latest = conn.execute(
                sa.text(
                    """
                    SELECT request_no, state FROM public.fomc_embedding_requests
                    WHERE run_id = :run ORDER BY request_no DESC LIMIT 1
                    """
                ),
                {"run": run.run_id},
            ).one_or_none()
            if latest is not None and latest.state != "succeeded":
                raise EmbeddingError("embedding_request_active")
            if _current_inputs(conn, run.scope) == run.inputs:
                remaining = _missing(conn, run)[:MAX_BATCH_INPUTS]
                if not remaining:
                    _finish_run(conn, run, "succeeded")
                    return None
                return self._reserve(
                    conn, run, remaining, 1 if latest is None else latest.request_no + 1
                )
            _finish_run(conn, run, "failed", "source_changed")
        raise EmbeddingError("source_changed")

    @staticmethod
    def _reserve(
        conn: Connection, run: EmbeddingRun, inputs: tuple[EmbeddingInput, ...], number: int
    ) -> EmbeddingBatch:
        batch = EmbeddingBatch(uuid4(), inputs)
        values = dict(
            request=batch.request_id,
            run=run.run_id,
            profile=run.profile_id,
            number=number,
            limit=run.request_limit,
            count=len(inputs),
        )
        conn.execute(
            sa.text(
                """
                INSERT INTO public.fomc_embedding_requests
                    (request_id, run_id, profile_id, request_limit, request_no, input_count, state)
                VALUES (:request, :run, :profile, :limit, :number, :count, 'reserved')
                """
            ),
            values,
        )
        conn.execute(
            sa.text(
                """
                INSERT INTO public.fomc_embedding_request_items
                    (request_id, profile_id, input_count, response_position,
                     passage_id, input_sha256)
                VALUES (:request, :profile, :count, :position, :passage, :digest)
                """
            ),
            [
                values | dict(position=index, passage=item.passage_id, digest=item.input_sha256)
                for index, item in enumerate(inputs)
            ],
        )
        return batch

    def mark_dispatched(self, run: EmbeddingRun, batch: EmbeddingBatch) -> None:
        with self._transaction() as conn:
            _lock_run(conn, run)
            _require_request(conn, run, batch, "reserved")
            conn.execute(
                sa.text(
                    """
                    UPDATE public.fomc_embedding_requests SET state = 'dispatched',
                        dispatch_at = clock_timestamp()
                    WHERE request_id = :request
                    """
                ),
                {"request": batch.request_id},
            )

    def publish(self, run: EmbeddingRun, batch: EmbeddingBatch, receipt: EmbeddingReceipt) -> int:
        with self._transaction() as conn:
            _lock_run(conn, run)
            _require_request(conn, run, batch, "dispatched")
            profile = load_profile(conn, run.profile_id)
            vectors = receipt.vectors(profile, len(batch.inputs))
            conn.execute(
                sa.text(
                    """
                    INSERT INTO public.fomc_passage_embeddings
                        (profile_id, passage_id, dimensions, request_id, response_position,
                         embedding, vector_sha256)
                    VALUES (:profile, :passage, :dimensions, :request, :position,
                            CAST(:embedding AS vector_support.vector), :digest)
                    """
                ),
                [
                    dict(
                        profile=run.profile_id,
                        passage=item.passage_id,
                        dimensions=profile.dimensions,
                        request=batch.request_id,
                        position=index,
                        embedding=vectors[index].postgres_value(),
                        digest=vectors[index].sha256,
                    )
                    for index, item in enumerate(batch.inputs)
                ],
            )
            conn.execute(
                sa.text(
                    """
                    UPDATE public.fomc_embedding_requests SET state = 'succeeded',
                        finished_at = clock_timestamp(), provider_request_id = :receipt,
                        returned_model = :model, prompt_tokens = :prompt, total_tokens = :total
                    WHERE request_id = :request
                    """
                ),
                dict(
                    receipt=receipt.request_id,
                    model=receipt.model,
                    prompt=receipt.prompt_tokens,
                    total=receipt.total_tokens,
                    request=batch.request_id,
                ),
            )
            if not _missing(conn, run):
                _finish_run(conn, run, "succeeded")
            return len(batch.inputs)

    def record_failure(
        self,
        run: EmbeddingRun,
        batch: EmbeddingBatch,
        failure: Failure,
        evidence: EmbeddingReceiptEvidence | None = None,
    ) -> None:
        if failure not in (
            "cancelled_before_dispatch",
            "response_invalid",
            "provider_rejected",
            "dispatch_uncertain",
        ):
            raise EmbeddingError("invalid_embedding_failure")
        uncertain = failure == "dispatch_uncertain"
        cancelled = failure == "cancelled_before_dispatch"
        if cancelled and evidence is not None:
            raise EmbeddingError("invalid_embedding_failure")
        observed = evidence or EmbeddingReceiptEvidence()
        with self._transaction() as conn:
            _lock_run(conn, run)
            _require_request(conn, run, batch, "reserved" if cancelled else "dispatched")
            conn.execute(
                sa.text(
                    """
                    UPDATE public.fomc_embedding_requests SET state = :state,
                        finished_at = clock_timestamp(), error_code = :error,
                        provider_request_id = :receipt, returned_model = :model,
                        prompt_tokens = :prompt, total_tokens = :total
                    WHERE request_id = :request
                    """
                ),
                dict(
                    state="unresolved" if uncertain else "failed",
                    error=failure,
                    request=batch.request_id,
                    receipt=observed.request_id,
                    model=observed.model,
                    prompt=observed.prompt_tokens,
                    total=observed.total_tokens,
                ),
            )
            _finish_run(
                conn,
                run,
                "reconciliation_required" if uncertain else "failed",
                "dispatch_uncertain"
                if uncertain
                else "cancelled"
                if cancelled
                else "request_failed",
            )
