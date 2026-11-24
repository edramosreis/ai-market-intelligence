from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.fomc_processing_store import FomcProcessingStore
from market_intelligence.db.fomc_store import FomcStore
from market_intelligence.fomc.artifacts import ArtifactStore
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.chunking import DEFAULT_CHUNKER_CONFIG, ChunkerConfig
from market_intelligence.fomc.models import DocumentKind, DocumentSpec
from market_intelligence.fomc.parsing import DEFAULT_PARSER_CONFIG
from market_intelligence.fomc.processing import process_pilot
from market_intelligence.fomc.retrieval import SearchRequest, rank_current, retrieval_snapshot
from market_intelligence.fomc.retrieval_models import RetrievalError
from market_intelligence.fomc.validation import validate_calendar, validate_document
from market_intelligence.time_utils import utc_now
from tests.fomc_fakes import fetched
from tests.fomc_passage_fakes import passage_body
from tests.integration.test_fomc_store import NOW, STATS, start, store  # noqa: F401

pytestmark = pytest.mark.integration
BODY = (
    "<p id='policy-section'>A synthetic policy passage describes interest rates and inflation.</p>"
    "<p>The second synthetic paragraph describes economic output and stable employment.</p>"
)


@pytest.fixture
def fomc_reader(database_settings: DatabaseSettings) -> Iterator[Engine]:
    engine = create_db_engine(database_settings, DatabaseRole.READ)
    try:
        yield engine
    finally:
        engine.dispose()


def collect_document(
    store: FomcStore,  # noqa: F811
    artifacts: ArtifactStore,
    spec: DocumentSpec,
    body: str = BODY,
    *,
    day: int = 0,
) -> str:
    at = NOW + timedelta(days=day)
    run_id, calendar = start(store, artifacts, at)
    raw = fetched(spec, passage_body(spec, body), at=at)
    artifacts.put(raw.body)
    store.record_document(
        run_id,
        validate_document(raw, validate_calendar(calendar.body)),
        calendar.receipt_id,
        artifacts,
        STATS,
    )
    store.finish(run_id, at, STATS, "interrupted")
    return raw.raw_version_id


def test_rank_stems_terms_and_uses_stable_ids_for_ties(
    store: FomcStore,  # noqa: F811
    fomc_reader: Engine,
    tmp_path: Path,
) -> None:
    artifacts = ArtifactStore(tmp_path)
    for spec in PILOT_DOCUMENTS:
        collect_document(store, artifacts, spec)
    process_pilot(FomcProcessingStore(store.engine), artifacts)
    with retrieval_snapshot(fomc_reader) as conn:
        found = rank_current(conn, SearchRequest("rates inflation", limit=2))
        assert found.coverage.ready == 4 and found.coverage.catalog_documents == 16
        assert found.coverage.not_collected == 12
        # Identical passage bodies have equal rank. One extra result exposes the item limit.
        assert len(found.candidates) == 3 and len({item.score for item in found.candidates}) == 1
        assert [item.passage_id for item in found.candidates] == sorted(
            item.passage_id for item in found.candidates
        )
        assert (
            rank_current(conn, SearchRequest("rate ultraviolet")).empty_status == "no_lexical_match"
        )
        released = rank_current(
            conn,
            SearchRequest(
                "interest rate", kind=DocumentKind.MINUTES, published_on=date(2024, 10, 9)
            ),
        )
        assert released.coverage.ready == 1 and len(released.candidates) == 1
        wrong_date = rank_current(
            conn,
            SearchRequest(
                "interest rate", kind=DocumentKind.MINUTES, published_on=date(2024, 9, 18)
            ),
        )
        assert wrong_date.empty_status == "no_documents_in_scope"
        with pytest.raises(RetrievalError, match="no_search_terms"):
            rank_current(conn, SearchRequest("the and of"))


def test_filtered_coverage_and_current_snapshot_have_no_old_fallback(
    store: FomcStore,  # noqa: F811
    fomc_reader: Engine,
    tmp_path: Path,
) -> None:
    artifacts = ArtifactStore(tmp_path)
    writer = FomcProcessingStore(store.engine)
    first, second, third, _ = PILOT_DOCUMENTS
    collect_document(store, artifacts, first)
    # A different chunker configuration leaves the default search corpus pending.
    process_pilot(writer, artifacts, chunker=ChunkerConfig(target_chars=120))
    with retrieval_snapshot(fomc_reader) as conn:
        result = rank_current(conn, SearchRequest("inflation"))
        assert result.empty_status == "corpus_not_ready" and result.coverage.pending == 1
    process_pilot(writer, artifacts)
    collect_document(store, artifacts, second)
    collect_document(store, artifacts, third)
    writer.failure(
        uuid4(),
        writer.inputs()[third.document_id],
        utc_now(),
        DEFAULT_PARSER_CONFIG,
        DEFAULT_CHUNKER_CONFIG,
        "artifact_error",
    )
    with retrieval_snapshot(fomc_reader) as conn:
        before = rank_current(conn, SearchRequest("inflation"))
        assert before.coverage.model_dump() == dict(
            catalog_documents=16,
            ready=1,
            pending=1,
            failed=1,
            not_collected=13,
        )
        assert len(before.candidates) == 1
        fresh = collect_document(
            store, artifacts, first, BODY + "<p>New source revision.</p>", day=1
        )
        # A search request retains the original visibility within its existing snapshot.
        assert rank_current(conn, SearchRequest("inflation")) == before
    with retrieval_snapshot(fomc_reader) as conn:
        after = rank_current(conn, SearchRequest("inflation"))
        assert after.empty_status == "corpus_not_ready" and after.coverage.pending == 2
    assert writer.inputs()[first.document_id].raw_version_id == fresh


def test_contact_passages_are_excluded_from_default_search(
    store: FomcStore,  # noqa: F811
    fomc_reader: Engine,
    tmp_path: Path,
) -> None:
    artifacts = ArtifactStore(tmp_path)
    spec = PILOT_DOCUMENTS[0]
    collect_document(
        store,
        artifacts,
        spec,
        BODY
        + (
            '<p>For media inquiries, please email <a href="mailto:evidence@example.test">'
            "support</a> about the synthetic report.</p>"
        ),
    )
    process_pilot(FomcProcessingStore(store.engine), artifacts)
    with retrieval_snapshot(fomc_reader) as conn:
        result = rank_current(conn, SearchRequest("media inquiries"))
        assert result.coverage.ready == 1 and result.empty_status == "no_lexical_match"
        assert result.candidates == ()
