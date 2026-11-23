import hashlib
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from market_intelligence.db.fomc_processing_store import FomcProcessingStore
from market_intelligence.db.fomc_processing_tables import fomc_parsed_blocks, fomc_passages
from market_intelligence.db.fomc_store import FomcStore
from market_intelligence.db.fomc_tables import fomc_fetch_receipts, fomc_raw_versions
from market_intelligence.fomc.artifacts import ArtifactStore
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.chunking import SEPARATOR
from market_intelligence.fomc.citations import FomcQueries
from market_intelligence.fomc.models import CALENDAR_URL, DocumentKind
from market_intelligence.fomc.processing import process_pilot
from market_intelligence.fomc.retrieval import SearchRequest
from market_intelligence.fomc.retrieval_models import RetrievalError
from tests.integration.test_fomc_retrieval import BODY, collect_document, fomc_reader  # noqa: F401
from tests.integration.test_fomc_store import NOW, store  # noqa: F401

pytestmark = pytest.mark.integration


def test_citations_resolve_exact_original_receipts_and_archive(
    store: FomcStore,  # noqa: F811
    fomc_reader: Engine,  # noqa: F811
    tmp_path: Path,
) -> None:
    artifacts = ArtifactStore(tmp_path)
    writer = FomcProcessingStore(store.engine)
    statement, minutes, *_ = PILOT_DOCUMENTS
    collect_document(store, artifacts, statement)
    unanchored = BODY.replace(" id='policy-section'", "")
    collect_document(store, artifacts, minutes, unanchored)
    results = process_pilot(writer, artifacts)
    assert [row.outcome for row in results.documents[:2]] == ["created", "created"]
    reader = FomcQueries(fomc_reader)
    result = reader.search(SearchRequest("interest rate", kind=DocumentKind.STATEMENT))
    hit = result.results[0]
    c = hit.citation
    assert c.citation_id == c.passage_id
    assert c.source_fragment == "policy-section"
    assert c.publication_basis == "statement_release"
    assert c.publication_evidence_receipt_id == c.source_receipt_id
    assert c.publication_evidence_sha256 == c.raw_sha256
    assert c.publication_evidence_url == c.official_url == statement.url
    assert c.passage_text_sha256 == hashlib.sha256(hit.text.encode()).hexdigest()
    assert c.source_received_at == NOW
    historical_id = c.passage_id
    current = reader.passage(historical_id)
    assert current.citation == c and current.text == hit.text and current.is_current

    hit = reader.search(SearchRequest("interest rate", kind=DocumentKind.MINUTES)).results[0]
    c = hit.citation
    assert c.meeting_end == date(2024, 9, 18) and c.published_on == date(2024, 10, 9)
    assert c.source_fragment is None and c.publication_basis == "calendar_release"
    assert c.publication_evidence_url == CALENDAR_URL and c.official_url == minutes.url
    with fomc_reader.connect() as conn:
        first, sha = conn.execute(
            sa.select(
                fomc_raw_versions.c.first_receipt_id,
                fomc_raw_versions.c.raw_sha256,
            ).where(fomc_raw_versions.c.raw_version_id == c.raw_version_id)
        ).one()
        linked_calendar = conn.execute(
            sa.select(
                fomc_fetch_receipts.c.calendar_receipt_id,
            ).where(fomc_fetch_receipts.c.receipt_id == first)
        ).scalar_one()
        assert c.source_receipt_id == first and c.raw_sha256 == sha
        assert c.publication_evidence_receipt_id == linked_calendar != first
        assert (
            c.publication_evidence_sha256
            == conn.execute(
                sa.select(
                    fomc_fetch_receipts.c.raw_sha256,
                ).where(fomc_fetch_receipts.c.receipt_id == linked_calendar)
            ).scalar_one()
        )

    collect_document(store, artifacts, statement, BODY + "<p>Changed original.</p>", day=1)
    archived = reader.passage(historical_id)
    assert not archived.is_current and archived.citation == current.citation
    assert archived.text == current.text
    new_search = reader.search(SearchRequest("rates", kind=DocumentKind.STATEMENT))
    assert new_search.status == "corpus_not_ready" and not new_search.results
    with pytest.raises(RetrievalError, match="passage_not_found"):
        reader.passage("0" * 64)


def test_lookup_pages_exact_unicode_spans_and_auxiliary_data(
    store: FomcStore,  # noqa: F811
    fomc_reader: Engine,  # noqa: F811
    tmp_path: Path,
) -> None:
    artifacts = ArtifactStore(tmp_path)
    collect_document(
        store,
        artifacts,
        PILOT_DOCUMENTS[0],
        BODY
        + (
            "<p>Résumé policy rate −2 percent.</p>"
            * 45
            + '<p>For media inquiries, please email <a href="mailto:evidence@example.test">'
            "support</a> about the synthetic report.</p>"
        ),
    )
    process_pilot(FomcProcessingStore(store.engine), artifacts)
    reader = FomcQueries(fomc_reader)
    hit = reader.search(SearchRequest("interest rate")).results[0]
    first = reader.passage(hit.citation.passage_id)
    assert len(first.spans) == 40 and first.next_span_start == 40
    second = reader.passage(hit.citation.passage_id, span_start=40)
    assert len(second.spans) == 7 and second.next_span_start is None
    assert first.text == second.text
    with fomc_reader.connect() as conn:
        bodies = dict(
            conn.execute(
                sa.select(
                    fomc_parsed_blocks.c.block_ordinal,
                    fomc_parsed_blocks.c.body,
                ).where(fomc_parsed_blocks.c.generation_id == first.citation.generation_id)
            )
            .tuples()
            .all()
        )
        aux = conn.execute(
            sa.select(fomc_passages.c.passage_id).where(
                fomc_passages.c.body.startswith("For media inquiries"),
            )
        ).scalar_one()
    assert (
        SEPARATOR.join(
            bodies[span.block_ordinal][span.start_offset : span.end_offset]
            for span in first.spans + second.spans
        )
        == first.text
    )
    assert "−2" in first.text
    assert reader.search(SearchRequest("media inquiries")).status == "no_lexical_match"
    assert reader.passage(aux).text.startswith("For media inquiries")
    with pytest.raises(RetrievalError, match="invalid_parameters"):
        reader.passage(hit.citation.passage_id, span_start=47)
