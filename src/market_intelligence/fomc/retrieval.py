"""Rank current FOMC policy passages in a read-only PostgreSQL snapshot."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from unicodedata import category

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from market_intelligence.db.fomc_processing_store import processing_status
from market_intelligence.db.fomc_processing_tables import (
    fomc_parsed_blocks as blocks,
)
from market_intelligence.db.fomc_processing_tables import (
    fomc_passage_spans as spans,
)
from market_intelligence.db.fomc_processing_tables import (
    fomc_passages as passages,
)
from market_intelligence.db.fomc_tables import fomc_documents as documents
from market_intelligence.fomc.catalog import CATALOG_IDS
from market_intelligence.fomc.chunking import DEFAULT_CHUNKER_CONFIG
from market_intelligence.fomc.isolation import require_fomc_database
from market_intelligence.fomc.models import DocumentKind
from market_intelligence.fomc.parsing import DEFAULT_PARSER_CONFIG
from market_intelligence.fomc.retrieval_models import (
    Coverage,
    NoMatchStatus,
    RetrievalError,
)

_ENGLISH = sa.literal_column("'pg_catalog.english'::regconfig", type_=sa.Text())
_POLICY_KINDS = ("paragraph", "quote_paragraph", "list_item")


@dataclass(frozen=True)
class SearchRequest:
    q: str
    limit: int = 5
    kind: DocumentKind | None = None
    meeting_end: date | None = None
    published_on: date | None = None

    def __post_init__(self) -> None:
        query = self.q.strip()
        if len(query) > 500 or any(category(char) == "Cc" for char in self.q):
            raise RetrievalError("invalid_parameters", 422)
        if not query:
            raise RetrievalError("no_search_terms", 422)
        if type(self.limit) is not int or not 1 <= self.limit <= 5:
            raise RetrievalError("invalid_parameters", 422)
        object.__setattr__(self, "q", query)


@dataclass(frozen=True)
class RankedCandidate:
    passage_id: str
    score: float


@dataclass(frozen=True)
class Ranking:
    coverage: Coverage
    candidates: tuple[RankedCandidate, ...]

    @property
    def empty_status(self) -> NoMatchStatus:
        if not self.coverage.catalog_documents:
            return "no_documents_in_scope"
        if not self.coverage.ready:
            return "corpus_not_ready"
        return "no_lexical_match"


@contextmanager
def retrieval_snapshot(engine: Engine) -> Iterator[Connection]:
    url = engine.url
    try:
        require_fomc_database(url)
    except ValueError:
        raise RetrievalError("retrieval_unavailable", 503) from None
    permitted = (
        {"m3_reader"}
        if url.database == "market_intelligence_m3"
        else {"m3_test_reader", "market_reader"}
    )
    if url.username not in permitted:
        raise RetrievalError("retrieval_unavailable", 503)
    with (
        engine.connect().execution_options(
            isolation_level="REPEATABLE READ", postgresql_readonly=True
        ) as conn,
        conn.begin(),
    ):
        role, db, readonly = conn.execute(
            sa.select(
                sa.func.current_user(),
                sa.func.current_database(),
                sa.func.current_setting("transaction_read_only"),
            )
        ).one()
        if role != url.username or db != url.database or readonly != "on":
            raise RetrievalError("retrieval_unavailable", 503)
        yield conn


def rank_current(conn: Connection, request: SearchRequest) -> Ranking:
    """Return at most limit + 1 candidates, including one to detect truncation."""
    query = sa.func.plainto_tsquery(_ENGLISH, sa.literal(request.q, type_=sa.Text()))
    if not conn.scalar(sa.select(sa.func.numnode(query))):
        raise RetrievalError("no_search_terms", 422)
    catalog = sa.select(documents.c.document_id).where(documents.c.document_id.in_(CATALOG_IDS))
    if request.kind is not None:
        catalog = catalog.where(documents.c.kind == request.kind.value)
    if request.meeting_end is not None:
        catalog = catalog.where(documents.c.meeting_end == request.meeting_end)
    if request.published_on is not None:
        catalog = catalog.where(documents.c.published_on == request.published_on)
    doc_ids: tuple[str, ...] = tuple(conn.scalars(catalog))
    states = processing_status(conn, DEFAULT_PARSER_CONFIG, DEFAULT_CHUNKER_CONFIG, doc_ids)
    counts = dict(not_collected=0, pending=0, failed=0, ready=0)
    set_ids: list[str] = []
    for row in states:
        counts[row["state"]] += 1
        if row["state"] == "ready":
            set_ids.append(row["passage_set_id"])
    coverage = Coverage(catalog_documents=len(states), **counts)
    if not set_ids:
        return Ranking(coverage, ())
    vector = sa.func.to_tsvector(_ENGLISH, passages.c.body)
    score = sa.func.ts_rank_cd(vector, query, 32).label("score")
    ranked = (
        sa.select(passages.c.passage_id, score)
        .select_from(
            passages.join(
                spans,
                sa.and_(
                    spans.c.passage_id == passages.c.passage_id,
                    spans.c.span_ordinal == 0,
                ),
            ).join(
                blocks,
                sa.and_(
                    blocks.c.generation_id == spans.c.generation_id,
                    blocks.c.block_ordinal == spans.c.block_ordinal,
                ),
            )
        )
        .where(
            passages.c.passage_set_id.in_(set_ids),
            blocks.c.kind.in_(_POLICY_KINDS),
            vector.bool_op("@@")(query),
        )
        .order_by(score.desc(), passages.c.passage_id.collate("C"))
        .limit(request.limit + 1)
    )
    return Ranking(
        coverage, tuple(RankedCandidate(row.passage_id, row.score) for row in conn.execute(ranked))
    )
