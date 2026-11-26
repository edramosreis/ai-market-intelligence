import os
from dataclasses import replace
from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import Connection, Engine

from market_intelligence.fomc import retrieval_models, vector_retrieval
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.citations import (
    FomcQueries,
    PublishedPassage,
    current_published_passages,
)
from market_intelligence.fomc.models import DocumentKind
from market_intelligence.fomc.retrieval import PassageScope
from market_intelligence.fomc.retrieval_models import RetrievalError
from market_intelligence.fomc.vector_db import register_embedding_profile
from market_intelligence.fomc.vector_models import EmbeddingOutput
from market_intelligence.fomc.vector_retrieval import search_fomc_vector
from tests.integration.test_fomc_embeddings import (
    PROFILE,
    VectorCase,
    receipt_for,
    vector_case,  # noqa: F401
)
from tests.integration.test_fomc_retrieval import (
    BODY,
    fomc_reader,  # noqa: F401
)
from tests.integration.test_fomc_store import store  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_VECTOR_TESTS") != "1", reason="Requires pgvector"),
]
AXIS = (1.0, 0.0, 0.0)


def publish_current(
    case: VectorCase, scope: PassageScope, *, last_orthogonal: bool = False
) -> tuple[str, ...]:
    writer = case.embeddings
    run = writer.start_run(PROFILE.profile_id, scope)
    assert run is not None
    batch = writer.reserve_next(run)
    assert batch is not None
    receipt = receipt_for(batch)
    if last_orthogonal:
        receipt = replace(
            receipt,
            outputs=[
                EmbeddingOutput(item.position, (0.0, 1.0, 0.0))
                if item.position == len(batch.inputs) - 1
                else item
                for item in receipt.outputs
            ],
        )
    writer.mark_dispatched(run, batch)
    writer.publish(run, batch, receipt)
    return tuple(item.passage_id for item in batch.inputs)


def test_exact_rank_ties_scope_citations_and_byte_bound(
    vector_case: VectorCase,  # noqa: F811
    fomc_reader: Engine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for spec in PILOT_DOCUMENTS:
        vector_case.collect(spec)
    ids = publish_current(vector_case, PassageScope(), last_orthogonal=True)
    assert len(ids) == 4
    all_hits = search_fomc_vector(fomc_reader, PROFILE.profile_id, AXIS, PassageScope())
    assert all_hits.status == "ok" and all_hits.coverage.ready == 4
    assert all_hits.coverage.not_collected == 12
    assert all_hits.eligible_passages == all_hits.embedded_passages == 4
    assert [hit.citation.passage_id for hit in all_hits.results] == list(ids)
    assert [hit.distance for hit in all_hits.results] == pytest.approx([0, 0, 0, 1])
    queries = FomcQueries(fomc_reader)
    for hit in all_hits.results:
        assert hit.citation == queries.passage(hit.citation.passage_id).citation
    short = search_fomc_vector(fomc_reader, PROFILE.profile_id, AXIS, PassageScope(), 1)
    assert short.results == all_hits.results[:1] and short.truncation_reason == "limit"
    meetings = PassageScope(kind=DocumentKind.MINUTES, meeting_end=date(2024, 9, 18))
    releases = PassageScope(kind=DocumentKind.MINUTES, published_on=date(2024, 10, 9))
    meeting = search_fomc_vector(fomc_reader, PROFILE.profile_id, AXIS, meetings)
    release = search_fomc_vector(fomc_reader, PROFILE.profile_id, AXIS, releases)
    assert meeting == release and len(release.results) == 1
    assert release.results[0].citation.meeting_end == meetings.meeting_end
    assert release.results[0].citation.published_on == releases.published_on
    empty = search_fomc_vector(
        fomc_reader, PROFILE.profile_id, AXIS, replace(releases, published_on=meetings.meeting_end)
    )
    assert empty.status == "no_documents_in_scope" and not empty.results
    monkeypatch.setattr(
        retrieval_models, "MAX_RESPONSE_BYTES", len(short.model_dump_json().encode()) + 32
    )
    trimmed = search_fomc_vector(fomc_reader, PROFILE.profile_id, AXIS, PassageScope())
    assert trimmed.results == short.results
    assert trimmed.has_more and trimmed.truncation_reason == "byte_budget"


def test_reader_reports_profile_gaps_and_preserves_citation_snapshot_during_correction(
    vector_case: VectorCase,  # noqa: F811
    fomc_reader: Engine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, *_ = PILOT_DOCUMENTS
    scope = PassageScope()
    empty = search_fomc_vector(fomc_reader, PROFILE.profile_id, AXIS, scope)
    assert empty.status == "corpus_not_ready"
    vector_case.collect(first)
    first_scope = PassageScope(kind=first.kind, published_on=first.published_on)
    (old_id,) = publish_current(vector_case, first_scope)
    vector_case.collect(second)
    partially_embedded = search_fomc_vector(fomc_reader, PROFILE.profile_id, AXIS, scope)
    assert partially_embedded.status == "vector_pending" and not partially_embedded.results
    assert (partially_embedded.eligible_passages, partially_embedded.embedded_passages) == (2, 1)
    other = register_embedding_profile(vector_case.admin, replace(PROFILE, epoch_id=UUID(int=2)))
    unembedded = search_fomc_vector(fomc_reader, other, AXIS, first_scope)
    assert unembedded.status == "vector_pending" and unembedded.embedded_passages == 0

    def during_search(conn: Connection, ids: tuple[str, ...]) -> tuple[PublishedPassage, ...]:
        vector_case.collect(first, BODY + "<p>A new synthetic source correction.</p>", day=1)
        return current_published_passages(conn, ids)

    with monkeypatch.context() as patch:
        patch.setattr(vector_retrieval, "current_published_passages", during_search)
        before = search_fomc_vector(fomc_reader, PROFILE.profile_id, AXIS, first_scope)
    assert before.status == "ok" and before.results[0].citation.passage_id == old_id
    after = search_fomc_vector(fomc_reader, PROFILE.profile_id, AXIS, first_scope)
    assert after.status == "vector_pending" and not after.results and after.embedded_passages == 0
    detail = FomcQueries(fomc_reader).passage(old_id)
    assert not detail.is_current and detail.citation == before.results[0].citation


def test_invalid_profile_and_vector_are_controlled(
    vector_case: VectorCase,  # noqa: F811
    fomc_reader: Engine,  # noqa: F811
) -> None:
    with pytest.raises(RetrievalError, match="^unknown_profile$") as missing:
        search_fomc_vector(fomc_reader, "a" * 64, AXIS, PassageScope())
    assert missing.value.status_code == 404
    with pytest.raises(RetrievalError, match="^invalid_vector$") as invalid:
        search_fomc_vector(fomc_reader, PROFILE.profile_id, [0.0, 0.0, 0.0], PassageScope())
    assert invalid.value.status_code == 422
    with pytest.raises(RetrievalError, match="^retrieval_unavailable$"):
        search_fomc_vector(vector_case.admin, PROFILE.profile_id, AXIS, PassageScope())
