"""Typed FOMC retrieval response and controlled reader failures."""

from datetime import date, datetime
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel

from market_intelligence.fomc.models import DocumentKind

MAX_RESPONSE_BYTES = 24 * 1024
NoMatchStatus = Literal["no_documents_in_scope", "corpus_not_ready", "no_lexical_match"]
VectorSearchStatus = Literal[
    "ok", "no_documents_in_scope", "corpus_not_ready", "no_eligible_passages", "vector_pending"
]


class RetrievalError(ValueError):
    def __init__(self, code: str, status_code: int) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class Coverage(BaseModel):
    catalog_documents: int
    not_collected: int
    pending: int
    failed: int
    ready: int


class SourceSpan(BaseModel):
    span_ordinal: int
    block_ordinal: int
    start_offset: int
    end_offset: int


class Citation(BaseModel):
    citation_id: str
    passage_id: str
    passage_set_id: str
    generation_id: str
    publisher: Literal["Board of Governors of the Federal Reserve System"]
    document_id: str
    kind: DocumentKind
    title: str
    official_url: str
    meeting_start: date
    meeting_end: date
    published_on: date
    publication_precision: Literal["date"]
    publication_basis: Literal["statement_release", "calendar_release"]
    publication_evidence_url: str
    publication_evidence_receipt_id: UUID
    publication_evidence_sha256: str
    raw_version_id: str
    raw_sha256: str
    source_receipt_id: UUID
    source_received_at: datetime
    parser_config_sha256: str
    chunker_config_sha256: str
    passage_text_sha256: str
    section_path: list[int]
    passage_ordinal: int
    first_span: SourceSpan
    last_span: SourceSpan
    span_count: int
    source_fragment: str | None


class SearchHit(BaseModel):
    text: str
    citation: Citation
    score: float


class SearchResult(BaseModel):
    results: list[SearchHit]
    coverage: Coverage
    status: Literal["matches"] | NoMatchStatus
    has_more: bool
    truncation_reason: Literal["limit", "byte_budget"] | None = None


class VectorSearchHit(BaseModel):
    text: str
    citation: Citation
    distance: float


class VectorSearchResult(BaseModel):
    results: list[VectorSearchHit]
    coverage: Coverage
    status: VectorSearchStatus
    profile_id: str
    eligible_passages: int
    embedded_passages: int
    has_more: bool
    truncation_reason: Literal["limit", "byte_budget"] | None = None


def bound_search_result[Result: SearchResult | VectorSearchResult](result: Result) -> Result:
    while len(result.model_dump_json().encode("utf-8")) > MAX_RESPONSE_BYTES:
        if len(result.results) <= 1:
            raise RetrievalError("evidence_unavailable", 503)
        result = cast(
            Result,
            result.model_copy(
                update=dict(
                    results=result.results[:-1], has_more=True, truncation_reason="byte_budget"
                )
            ),
        )
    return result


class PassageDetail(BaseModel):
    text: str
    citation: Citation
    is_current: bool
    spans: list[SourceSpan]
    next_span_start: int | None
