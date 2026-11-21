"""Resolve published passages to their exact stored source evidence."""

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from unicodedata import category

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy import Connection, Engine
from sqlalchemy.engine import RowMapping

from market_intelligence.db.fomc_processing_store import PILOT_IDS
from market_intelligence.db.fomc_processing_tables import (
    fomc_parse_generations as generations,
)
from market_intelligence.db.fomc_processing_tables import (
    fomc_parsed_blocks as blocks,
)
from market_intelligence.db.fomc_processing_tables import (
    fomc_passage_sets as sets,
)
from market_intelligence.db.fomc_processing_tables import (
    fomc_passage_spans as spans,
)
from market_intelligence.db.fomc_processing_tables import (
    fomc_passages as passages,
)
from market_intelligence.db.fomc_processing_tables import (
    fomc_processing_attempts as attempts,
)
from market_intelligence.db.fomc_tables import (
    fomc_current as current,
)
from market_intelligence.db.fomc_tables import (
    fomc_documents as documents,
)
from market_intelligence.db.fomc_tables import (
    fomc_fetch_receipts as receipts,
)
from market_intelligence.db.fomc_tables import (
    fomc_raw_versions as versions,
)
from market_intelligence.fomc.chunking import DEFAULT_CHUNKER_CONFIG, SEPARATOR
from market_intelligence.fomc.models import DocumentKind
from market_intelligence.fomc.parsing import DEFAULT_PARSER_CONFIG
from market_intelligence.fomc.retrieval import SearchRequest, rank_current, retrieval_snapshot
from market_intelligence.fomc.retrieval_models import (
    Citation,
    PassageDetail,
    RetrievalError,
    SearchHit,
    SearchResult,
    SourceSpan,
)

SPAN_PAGE_SIZE = 40
_PUBLISHER = "Board of Governors of the Federal Reserve System"
_PASSAGE_ID = re.compile(r"[0-9a-f]{64}")
_FIRST = receipts.alias("source_receipt")
_CALENDAR = receipts.alias("publication_calendar")


def _unavailable() -> RetrievalError:
    return RetrievalError("evidence_unavailable", 503)


def _published_rows(conn: Connection, passage_ids: tuple[str, ...]) -> dict[str, RowMapping]:
    selection = (
        sa.select(
            passages.c.passage_id,
            passages.c.passage_set_id,
            passages.c.generation_id,
            passages.c.passage_ordinal,
            passages.c.body,
            passages.c.section_path,
            passages.c.text_sha256.label("passage_text_sha256"),
            generations.c.parser_config_sha256,
            sets.c.chunker_config_sha256,
            documents.c.document_id,
            documents.c.kind,
            documents.c.official_url,
            documents.c.meeting_start,
            documents.c.meeting_end,
            documents.c.published_on,
            documents.c.publication_precision,
            documents.c.publication_evidence_url,
            versions.c.raw_version_id,
            versions.c.raw_sha256,
            current.c.raw_version_id.label("current_raw_version_id"),
            _FIRST.c.receipt_id.label("source_receipt_id"),
            _FIRST.c.received_at.label("source_received_at"),
            _FIRST.c.final_url.label("source_receipt_url"),
            _FIRST.c.verified_title.label("title"),
            _CALENDAR.c.receipt_id.label("calendar_receipt_id"),
            _CALENDAR.c.final_url.label("calendar_receipt_url"),
            _CALENDAR.c.raw_sha256.label("calendar_sha256"),
        )
        .select_from(
            passages.join(sets, passages.c.passage_set_id == sets.c.passage_set_id)
            .join(generations, sets.c.generation_id == generations.c.generation_id)
            .join(versions, generations.c.raw_version_id == versions.c.raw_version_id)
            .join(documents, versions.c.document_id == documents.c.document_id)
            .outerjoin(current, current.c.document_id == documents.c.document_id)
            .join(
                attempts,
                sa.and_(
                    attempts.c.passage_set_id == sets.c.passage_set_id,
                    attempts.c.outcome == "created",
                ),
            )
            # Left joins let missing provenance fail as controlled inconsistent evidence.
            .outerjoin(
                _FIRST,
                sa.and_(
                    versions.c.first_receipt_id == _FIRST.c.receipt_id,
                    _FIRST.c.raw_version_id == versions.c.raw_version_id,
                    _FIRST.c.document_id == documents.c.document_id,
                    _FIRST.c.raw_sha256 == versions.c.raw_sha256,
                    _FIRST.c.resource_kind == "document",
                ),
            )
            .outerjoin(
                _CALENDAR,
                sa.and_(
                    _FIRST.c.calendar_receipt_id == _CALENDAR.c.receipt_id,
                    _CALENDAR.c.run_id == _FIRST.c.run_id,
                    _CALENDAR.c.resource_kind == "calendar",
                ),
            )
        )
        .where(documents.c.document_id.in_(PILOT_IDS), passages.c.passage_id.in_(passage_ids))
    )
    return {row["passage_id"]: row for row in conn.execute(selection).mappings()}


def _span_rows(passage_id: str, *, include_first_anchors: bool = False) -> sa.Select[Any]:
    selection = (
        sa.select(
            spans.c.span_ordinal,
            spans.c.block_ordinal,
            spans.c.start_offset,
            spans.c.end_offset,
            sa.func.char_length(blocks.c.body).label("block_length"),
        )
        .select_from(
            spans.join(
                blocks,
                sa.and_(
                    spans.c.generation_id == blocks.c.generation_id,
                    spans.c.block_ordinal == blocks.c.block_ordinal,
                ),
            )
        )
        .where(spans.c.passage_id == passage_id)
        .order_by(spans.c.span_ordinal)
    )
    if include_first_anchors:
        return selection.add_columns(
            sa.case((spans.c.span_ordinal == 0, blocks.c.anchors)).label("anchors")
        )
    return selection


def _span(row: RowMapping) -> SourceSpan:
    if not (0 <= row["start_offset"] < row["end_offset"] <= row["block_length"]):
        raise _unavailable()
    return SourceSpan(
        span_ordinal=row["span_ordinal"],
        block_ordinal=row["block_ordinal"],
        start_offset=row["start_offset"],
        end_offset=row["end_offset"],
    )


def _fragment(first: RowMapping) -> str | None:
    anchors = first["anchors"]
    if not isinstance(anchors, list):
        raise _unavailable()
    names: list[str] = []
    for anchor in anchors:
        if not isinstance(anchor, dict) or not isinstance(anchor.get("name"), str):
            raise _unavailable()
        if anchor.get("offset") == first["start_offset"]:
            names.append(anchor["name"])
    if len(names) != 1:
        return None
    name = names[0]
    return (
        name
        if (1 <= len(name) <= 256 and not any(c.isspace() or category(c) == "Cc" for c in name))
        else None
    )


@dataclass(frozen=True)
class PublishedPassage:
    text: str
    citation: Citation
    is_current: bool


def _citation(conn: Connection, row: RowMapping) -> PublishedPassage:
    passage_id = row["passage_id"]
    body = row["body"]
    if (
        not 1 <= len(body) <= 2400
        or hashlib.sha256(body.encode("utf-8")).hexdigest() != row["passage_text_sha256"]
        or row["source_receipt_url"] != row["official_url"]
        or row["source_receipt_id"] is None
        or not row["title"]
    ):
        raise _unavailable()
    count, lower, upper = conn.execute(
        sa.select(
            sa.func.count(), sa.func.min(spans.c.span_ordinal), sa.func.max(spans.c.span_ordinal)
        ).where(spans.c.passage_id == passage_id)
    ).one()
    # Even one-character spans must fit with the separator into the stored body.
    max_spans = (len(body) + len(SEPARATOR)) // (1 + len(SEPARATOR))
    if not (1 <= count <= max_spans and lower == 0 and upper == count - 1):
        raise _unavailable()
    ends = (
        conn.execute(
            _span_rows(passage_id, include_first_anchors=True).where(
                spans.c.span_ordinal.in_((0, upper))
            )
        )
        .mappings()
        .all()
    )
    if len(ends) != (1 if count == 1 else 2):
        raise _unavailable()
    is_statement = row["kind"] == DocumentKind.STATEMENT
    evidence_id = row["source_receipt_id"] if is_statement else row["calendar_receipt_id"]
    evidence_sha = row["raw_sha256"] if is_statement else row["calendar_sha256"]
    evidence_url = row["source_receipt_url"] if is_statement else row["calendar_receipt_url"]
    if (
        evidence_id is None
        or evidence_sha is None
        or evidence_url != row["publication_evidence_url"]
    ):
        raise _unavailable()
    citation_values = {key: row[key] for key in Citation.model_fields if key in row}
    citation_values.update(
        citation_id=passage_id,
        publisher=_PUBLISHER,
        publication_basis="statement_release" if is_statement else "calendar_release",
        publication_evidence_receipt_id=evidence_id,
        publication_evidence_sha256=evidence_sha,
        first_span=_span(ends[0]),
        last_span=_span(ends[-1]),
        span_count=count,
        source_fragment=_fragment(ends[0]),
    )
    try:
        citation = Citation.model_validate(citation_values)
    except ValidationError:
        raise _unavailable() from None
    is_current = (
        row["current_raw_version_id"] == citation.raw_version_id
        and citation.parser_config_sha256 == DEFAULT_PARSER_CONFIG.config_hash
        and citation.chunker_config_sha256 == DEFAULT_CHUNKER_CONFIG.config_hash
    )
    return PublishedPassage(body, citation, is_current)


class FomcQueries:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def search(self, request: SearchRequest) -> SearchResult:
        with retrieval_snapshot(self.engine) as conn:
            ranking = rank_current(conn, request)
            if not ranking.candidates:
                return SearchResult(
                    results=[],
                    coverage=ranking.coverage,
                    status=ranking.empty_status,
                    has_more=False,
                )
            chosen = ranking.candidates[: request.limit]
            rows = _published_rows(conn, tuple(candidate.passage_id for candidate in chosen))
            hits: list[SearchHit] = []
            for candidate in chosen:
                if candidate.passage_id not in rows:
                    raise _unavailable()
                published = _citation(conn, rows[candidate.passage_id])
                if not published.is_current:
                    raise _unavailable()
                hits.append(
                    SearchHit(
                        text=published.text,
                        citation=published.citation,
                        score=candidate.score,
                    )
                )
            more = len(ranking.candidates) > request.limit
            return SearchResult(
                results=hits,
                coverage=ranking.coverage,
                status="matches",
                has_more=more,
                truncation_reason="limit" if more else None,
            )

    def passage(self, passage_id: str, span_start: int = 0) -> PassageDetail:
        if (
            _PASSAGE_ID.fullmatch(passage_id) is None
            or type(span_start) is not int
            or span_start < 0
        ):
            raise RetrievalError("invalid_parameters", 422)
        with retrieval_snapshot(self.engine) as conn:
            row = _published_rows(conn, (passage_id,)).get(passage_id)
            if row is None:
                raise RetrievalError("passage_not_found", 404)
            published = _citation(conn, row)
            count = published.citation.span_count
            if span_start >= count:
                raise RetrievalError("invalid_parameters", 422)
            page_end = min(span_start + SPAN_PAGE_SIZE, count)
            rows = (
                conn.execute(
                    _span_rows(passage_id).where(
                        spans.c.span_ordinal >= span_start,
                        spans.c.span_ordinal < page_end,
                    )
                )
                .mappings()
                .all()
            )
            if [row["span_ordinal"] for row in rows] != list(range(span_start, page_end)):
                raise _unavailable()
            return PassageDetail(
                text=published.text,
                citation=published.citation,
                is_current=published.is_current,
                spans=[_span(row) for row in rows],
                next_span_start=page_end if page_end < count else None,
            )
