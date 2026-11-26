import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from market_intelligence.db.fomc_embedding_store import (
    EmbeddingBatch,
    EmbeddingRun,
    FomcEmbeddingStore,
)
from market_intelligence.db.fomc_processing_store import FomcProcessingStore
from market_intelligence.db.fomc_store import FomcStore
from market_intelligence.fomc.artifacts import ArtifactStore
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.models import DocumentSpec
from market_intelligence.fomc.processing import process_pilot
from market_intelligence.fomc.retrieval import PassageScope
from market_intelligence.fomc.vector_db import register_embedding_profile
from market_intelligence.fomc.vector_models import (
    EmbeddingError,
    EmbeddingOutput,
    EmbeddingProfile,
    EmbeddingReceipt,
    EmbeddingReceiptEvidence,
    canonical_vector,
)
from tests.integration.test_fomc_retrieval import BODY, collect_document
from tests.integration.test_fomc_store import store  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_VECTOR_TESTS") != "1", reason="Requires pgvector"),
]
PROFILE = EmbeddingProfile("synthetic", "fixture", 3, "fixture", "one", UUID(int=1))


@dataclass(frozen=True)
class VectorCase:
    sources: FomcStore
    embeddings: FomcEmbeddingStore
    artifacts: ArtifactStore
    admin: Engine

    def collect(self, spec: DocumentSpec, body: str = BODY, *, day: int = 0) -> None:
        collect_document(self.sources, self.artifacts, spec, body, day=day)
        process_pilot(FomcProcessingStore(self.sources.engine), self.artifacts)


@pytest.fixture
def vector_case(store: FomcStore, admin_engine: Engine, tmp_path: Path) -> VectorCase:  # noqa: F811
    register_embedding_profile(admin_engine, PROFILE)
    return VectorCase(
        store, FomcEmbeddingStore(store.engine), ArtifactStore(tmp_path), admin_engine
    )


def receipt_for(batch: EmbeddingBatch) -> EmbeddingReceipt:
    return EmbeddingReceipt(
        f"fixture-{batch.request_id}",
        PROFILE.model,
        7,
        7,
        [EmbeddingOutput(index, [1.0, 0.0, -0.0]) for index in reversed(range(len(batch.inputs)))],
    )


def test_reservation_publication_and_replay_preserve_exact_provenance(
    vector_case: VectorCase,
) -> None:
    first, second, *_ = PILOT_DOCUMENTS
    vector_case.collect(first)
    vector_case.collect(second)
    writer = vector_case.embeddings
    assert register_embedding_profile(vector_case.admin, PROFILE) == PROFILE.profile_id
    run = writer.start_run(PROFILE.profile_id, PassageScope(), request_limit=1)
    assert run is not None
    batch = writer.reserve_next(run)
    assert batch is not None and len(batch.inputs) >= 2
    with pytest.raises(EmbeddingError, match="embedding_request_unavailable"):
        writer.publish(run, batch, receipt_for(batch))
    with pytest.raises(EmbeddingError, match="embedding_request_active"):
        writer.reserve_next(run)
    writer.mark_dispatched(run, batch)
    assert writer.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    invalid = replace(receipt_for(batch), outputs=[EmbeddingOutput(0, [1.0, 0.0, 0.0])])
    with pytest.raises(EmbeddingError, match="invalid_embedding_response"):
        writer.publish(run, batch, invalid)
    guard = sa.text(
        "UPDATE public.fomc_embedding_requests SET state = 'succeeded', "
        "finished_at = clock_timestamp(), provider_request_id = 'fixture', "
        "returned_model = 'fixture', prompt_tokens = 7, total_tokens = 7"
    )
    with (
        pytest.raises(IntegrityError, match="Incomplete FOMC embedding publication"),
        writer.engine.begin() as conn,
    ):
        conn.execute(guard)
    with vector_case.admin.connect() as conn:
        assert conn.scalar(sa.text("SELECT count(*) FROM fomc_passage_embeddings")) == 0
        assert conn.scalar(sa.text("SELECT state FROM fomc_embedding_requests")) == "dispatched"
    assert writer.publish(run, batch, receipt_for(batch)) == len(batch.inputs)
    assert writer.start_run(PROFILE.profile_id, PassageScope()) is None
    with vector_case.admin.connect() as conn:
        rows = conn.exec_driver_sql(
            """
            SELECT e.response_position, e.passage_id, i.input_sha256, e.vector_sha256,
                   r.provider_request_id, r.total_tokens, r.state, run.state
            FROM fomc_passage_embeddings AS e
            JOIN fomc_embedding_request_items AS i USING (request_id, response_position)
            JOIN fomc_embedding_requests AS r USING (request_id)
            JOIN fomc_embedding_runs AS run USING (run_id)
            ORDER BY e.response_position
            """
        ).all()
    digest = canonical_vector([1.0, 0, 0], 3).sha256
    assert rows == [
        (
            index,
            item.passage_id,
            item.input_sha256,
            digest,
            receipt_for(batch).request_id,
            7,
            "succeeded",
            "succeeded",
        )
        for index, item in enumerate(batch.inputs)
    ]


def test_concurrent_run_reservation_and_uncertain_dispatch_hold_one_profile(
    vector_case: VectorCase,
) -> None:
    vector_case.collect(PILOT_DOCUMENTS[0])
    writer = vector_case.embeddings
    barrier = Barrier(2)

    def start() -> object:
        barrier.wait(timeout=10)
        try:
            return writer.start_run(PROFILE.profile_id, PassageScope())
        except EmbeddingError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = tuple(pool.map(lambda _: start(), range(2)))
    assert "embedding_run_active" in attempts
    runs = [item for item in attempts if isinstance(item, EmbeddingRun)]
    assert len(runs) == 1
    run = runs[0]
    batch = writer.reserve_next(run)
    assert batch is not None
    writer.mark_dispatched(run, batch)
    writer.record_failure(run, batch, "dispatch_uncertain", EmbeddingReceiptEvidence("synthetic"))
    with pytest.raises(EmbeddingError, match="embedding_run_active"):
        writer.start_run(PROFILE.profile_id, PassageScope())
    with vector_case.admin.connect() as conn:
        assert (
            conn.scalar(sa.text("SELECT state FROM fomc_embedding_runs"))
            == "reconciliation_required"
        )
        assert conn.execute(
            sa.text("SELECT state, provider_request_id, total_tokens FROM fomc_embedding_requests")
        ).one() == ("unresolved", "synthetic", None)


def test_new_run_reuses_previous_outputs_after_confirmed_failure(vector_case: VectorCase) -> None:
    first, second, *_ = PILOT_DOCUMENTS
    vector_case.collect(first)
    writer = vector_case.embeddings
    first_run = writer.start_run(PROFILE.profile_id, PassageScope())
    assert first_run is not None
    previous = writer.reserve_next(first_run)
    assert previous is not None
    writer.mark_dispatched(first_run, previous)
    writer.publish(first_run, previous, receipt_for(previous))
    vector_case.collect(second)
    for cancel in (True, False):
        next_run = writer.start_run(PROFILE.profile_id, PassageScope())
        assert next_run is not None
        batch = writer.reserve_next(next_run)
        assert batch is not None and set(previous.inputs).isdisjoint(batch.inputs)
        if cancel:
            writer.record_failure(next_run, batch, "cancelled_before_dispatch")
        else:
            writer.mark_dispatched(next_run, batch)
            writer.publish(next_run, batch, receipt_for(batch))
    assert writer.start_run(PROFILE.profile_id, PassageScope()) is None
