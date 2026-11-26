"""Internal exact cosine retrieval from precomputed FOMC embedding coordinates."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from market_intelligence.db.fomc_vector_schema import EMBEDDING_TABLE
from market_intelligence.fomc.citations import current_published_passages
from market_intelligence.fomc.retrieval import (
    PassageScope,
    current_policy_passages,
    retrieval_snapshot,
)
from market_intelligence.fomc.retrieval_models import (
    RetrievalError,
    VectorSearchHit,
    VectorSearchResult,
    VectorSearchStatus,
    bound_search_result,
)
from market_intelligence.fomc.vector_db import (
    check_vector_connection,
    load_profile,
    require_vector_target,
)
from market_intelligence.fomc.vector_models import EmbeddingError, canonical_vector

_EMBEDDINGS = sa.table(
    EMBEDDING_TABLE,
    sa.column("profile_id", sa.Text()),
    sa.column("passage_id", sa.Text()),
    sa.column("embedding"),
    schema="public",
)
_INPUT_ERRORS = {"invalid_profile": 422, "invalid_vector": 422, "unknown_profile": 404}


def search_fomc_vector(
    engine: Engine,
    profile_id: str,
    precomputed_vector: Sequence[float],
    scope: PassageScope,
    limit: int = 5,
) -> VectorSearchResult:
    """Search current policy passages in one reader-only snapshot."""
    if type(limit) is not int or not 1 <= limit <= 5:
        raise RetrievalError("invalid_parameters", 422)
    try:
        require_vector_target(engine, "reader")
        with retrieval_snapshot(engine) as conn:
            check_vector_connection(conn, engine)
            profile = load_profile(conn, profile_id)
            query = canonical_vector(precomputed_vector, profile.dimensions)
            return _search(conn, profile_id, query.postgres_value(), scope, limit)
    except EmbeddingError as error:
        code = str(error)
        if code in _INPUT_ERRORS:
            raise RetrievalError(code, _INPUT_ERRORS[code]) from None
        raise RetrievalError("retrieval_unavailable", 503) from None
    except sa.exc.SQLAlchemyError:
        raise RetrievalError("retrieval_unavailable", 503) from None


def _search(
    conn: Connection, profile_id: str, vector: str, scope: PassageScope, limit: int
) -> VectorSearchResult:
    coverage, selection = current_policy_passages(conn, scope)
    policy = selection.subquery()
    scope_with_embeddings = policy.outerjoin(
        _EMBEDDINGS,
        sa.and_(
            _EMBEDDINGS.c.passage_id == policy.c.passage_id, _EMBEDDINGS.c.profile_id == profile_id
        ),
    )
    eligible, embedded = conn.execute(
        sa.select(sa.func.count(), sa.func.count(_EMBEDDINGS.c.passage_id)).select_from(
            scope_with_embeddings
        )
    ).one()
    status: VectorSearchStatus = "ok"
    if not coverage.catalog_documents:
        status = "no_documents_in_scope"
    elif not coverage.ready:
        status = "corpus_not_ready"
    elif not eligible:
        status = "no_eligible_passages"
    elif embedded != eligible:
        status = "vector_pending"
    hits: list[VectorSearchHit] = []
    has_more = False
    if status == "ok":
        distance = _EMBEDDINGS.c.embedding.op(
            "OPERATOR(vector_support.<=>)", return_type=sa.Float()
        )(sa.text("CAST(:query_vector AS vector_support.vector)")).label("distance")
        ranked = conn.execute(
            sa.select(policy.c.passage_id, distance)
            .select_from(scope_with_embeddings)
            .order_by(distance, policy.c.passage_id.collate("C"))
            .limit(limit + 1),
            {"query_vector": vector},
        ).all()
        chosen = ranked[:limit]
        passages = current_published_passages(conn, tuple(row.passage_id for row in chosen))
        hits = [
            VectorSearchHit(text=passage.text, citation=passage.citation, distance=row.distance)
            for row, passage in zip(chosen, passages, strict=True)
        ]
        has_more = len(ranked) > limit
    return bound_search_result(
        VectorSearchResult(
            results=hits,
            coverage=coverage,
            status=status,
            profile_id=profile_id,
            eligible_passages=eligible,
            embedded_passages=embedded,
            has_more=has_more,
            truncation_reason="limit" if has_more else None,
        )
    )
