from collections.abc import Iterator
from datetime import UTC, date, datetime
from unittest.mock import MagicMock
from uuid import UUID

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from market_intelligence.api import create_app
from market_intelligence.fomc import retrieval_routes
from market_intelligence.fomc.models import DocumentKind
from market_intelligence.fomc.retrieval_models import (
    Citation,
    Coverage,
    PassageDetail,
    SearchHit,
    SearchResult,
    SourceSpan,
)

SEARCH = "/v1/fomc/passages/search"
ID = "a" * 64


@pytest.fixture
def client_and_reader() -> Iterator[tuple[TestClient, MagicMock]]:
    engine = sa.create_engine("postgresql+psycopg://sample@db/market_intelligence")
    app = create_app(engine=engine)
    try:
        with TestClient(app) as client:
            mock = MagicMock()
            app.state.fomc_queries = mock
            yield client, mock
    finally:
        engine.dispose()


def at(ordinal: int) -> SourceSpan:
    return SourceSpan(span_ordinal=ordinal, block_ordinal=ordinal, start_offset=0, end_offset=1)


def citation(passage_id: str = ID, span_count: int = 1) -> Citation:
    receipt = UUID(int=1)
    return Citation(
        citation_id=passage_id,
        passage_id=passage_id,
        passage_set_id=ID,
        generation_id=ID,
        publisher="Board of Governors of the Federal Reserve System",
        document_id=ID,
        kind=DocumentKind.STATEMENT,
        title="Synthetic policy statement",
        official_url="https://www.federalreserve.gov/example.htm",
        meeting_start=date(2024, 9, 17),
        meeting_end=date(2024, 9, 18),
        published_on=date(2024, 9, 18),
        publication_precision="date",
        publication_basis="statement_release",
        publication_evidence_url="https://www.federalreserve.gov/example.htm",
        publication_evidence_receipt_id=receipt,
        publication_evidence_sha256=ID,
        raw_version_id=ID,
        raw_sha256=ID,
        source_receipt_id=receipt,
        source_received_at=datetime(2026, 10, 21, tzinfo=UTC),
        parser_config_sha256=ID,
        chunker_config_sha256=ID,
        passage_text_sha256=ID,
        section_path=[],
        passage_ordinal=0,
        first_span=at(0),
        last_span=at(span_count - 1),
        span_count=span_count,
        source_fragment=None,
    )


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?q",
        "?q=inflation&q=rate",
        "?q=inflation&limit=0",
        "?q=rate&limit=5.0",
        "?q=rate&limit=6",
        "?q=rate&kind=press",
        "?q=rate&published_on=2024-02-30",
        "?q=rate&meeting_end=20240918",
        "?q=rate&source=http://example.test",
        "?q=a%00b",
        "?q=%09rate",
        "?q=" + "x" * 501,
    ],
)
def test_invalid_search_does_not_open_reader(
    client_and_reader: tuple[TestClient, MagicMock],
    query: str,
) -> None:
    client, reader = client_and_reader
    response = client.get(SEARCH + query)
    assert response.status_code == 422
    assert response.json()["detail"] in {"invalid_parameters", "no_search_terms"}
    assert response.headers["cache-control"] == "no-store"
    reader.search.assert_not_called()


def test_query_character_limit_applies_after_trimming(
    client_and_reader: tuple[TestClient, MagicMock],
) -> None:
    client, reader = client_and_reader
    reader.search.return_value = SearchResult(
        results=[],
        coverage=Coverage(catalog_documents=0, ready=0, not_collected=0, pending=0, failed=0),
        status="no_documents_in_scope",
        has_more=False,
    )
    query = "é" * 500
    response = client.get(SEARCH, params={"q": f"  {query}  "})
    assert response.status_code == 200
    assert reader.search.call_args.args[0].q == query


def test_search_budget_measures_actual_unicode_response_and_keeps_rank_prefix(
    client_and_reader: tuple[TestClient, MagicMock],
) -> None:
    client, reader = client_and_reader
    text = "📈" * 2400
    hits = [SearchHit(text=text, citation=citation(f"{i:064x}"), score=1 / i) for i in range(1, 6)]
    reader.search.return_value = SearchResult(
        results=hits,
        coverage=Coverage(catalog_documents=4, ready=4, not_collected=0, pending=0, failed=0),
        status="matches",
        has_more=False,
    )
    response = client.get(SEARCH, params={"q": " test ", "kind": "minutes", "limit": 5})
    assert response.status_code == 200 and len(response.content) <= 24 * 1024
    value = SearchResult.model_validate_json(response.content)
    assert value.results == hits[:2]
    assert value.has_more and value.truncation_reason == "byte_budget"
    assert reader.search.call_args.args[0].q == "test"


def test_detail_budget_advances_by_the_last_actually_returned_span(
    client_and_reader: tuple[TestClient, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, reader = client_and_reader
    spans = [at(i) for i in range(40, 45)]
    page = PassageDetail(
        text="Résumé",
        citation=citation(span_count=45),
        is_current=False,
        spans=spans,
        next_span_start=None,
    )
    expected = page.model_copy(update={"spans": spans[:2], "next_span_start": 42})
    monkeypatch.setattr(
        retrieval_routes, "MAX_RESPONSE_BYTES", len(expected.model_dump_json().encode("utf-8"))
    )
    reader.passage.return_value = page
    response = client.get(f"/v1/fomc/passages/{ID}", params={"span_start": 40})
    assert response.status_code == 200 and response.json() == expected.model_dump(mode="json")
    reader.passage.assert_called_once_with(ID, span_start=40)


def test_database_guard_refuses_non_m3_before_opening_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = sa.create_engine("postgresql+psycopg://market_reader@db/market_intelligence")
    forbidden = MagicMock(side_effect=AssertionError("Do not open demo DB"))
    monkeypatch.setattr(engine, "connect", forbidden)
    with TestClient(create_app(engine=engine)) as client:
        response = client.get(SEARCH, params={"q": "inflation"})
    assert response.status_code == 503 and response.json() == {"detail": "retrieval_unavailable"}
    forbidden.assert_not_called()
    engine.dispose()
