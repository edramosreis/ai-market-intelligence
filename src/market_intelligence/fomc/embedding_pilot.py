"""Publish the approved corpus and compare fixed queries without a public API."""

from collections import deque
from collections.abc import Callable, Sequence
from time import monotonic, sleep
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import Engine

from market_intelligence.db.fomc_embedding_store import (
    EmbeddingBatch,
    EmbeddingRun,
    FomcEmbeddingStore,
)
from market_intelligence.fomc.citations import FomcQueries
from market_intelligence.fomc.embedding_pilot_ledger import PilotLedger, RequestKind
from market_intelligence.fomc.embedding_pilot_plan import PilotPlan
from market_intelligence.fomc.embedding_provider import EmbeddingDispatchError, token_count
from market_intelligence.fomc.retrieval import PassageScope, SearchRequest
from market_intelligence.fomc.retrieval_models import SearchHit, VectorSearchHit
from market_intelligence.fomc.vector_db import register_embedding_profile
from market_intelligence.fomc.vector_models import (
    EmbeddingError,
    EmbeddingProfile,
    EmbeddingReceipt,
    sha256,
)
from market_intelligence.fomc.vector_retrieval import search_fomc_vector


class EmbeddingProvider(Protocol):
    def embed(
        self, profile: EmbeddingProfile, inputs: Sequence[Sequence[int]], request_id: UUID
    ) -> EmbeddingReceipt: ...


class PilotRateLimit:
    def __init__(
        self,
        requests_per_minute: int,
        tokens_per_minute: int,
        *,
        now: Callable[[], float] = monotonic,
        wait: Callable[[float], None] = sleep,
    ) -> None:
        if requests_per_minute < 1 or tokens_per_minute < 1:
            raise EmbeddingError("invalid_pilot_rate_limit")
        self._requests = requests_per_minute
        self._tokens = tokens_per_minute
        self._now = now
        self._wait = wait
        self._window: deque[tuple[float, int]] = deque()

    def __call__(self, count: int) -> None:
        if not 1 <= count <= self._tokens:
            raise EmbeddingError("pilot_batch_exceeds_rate_limit")
        while True:
            now = self._now()
            while self._window and now - self._window[0][0] >= 60.0:
                self._window.popleft()
            if (
                len(self._window) < self._requests
                and sum(n for _, n in self._window) + count <= self._tokens
            ):
                self._window.append((now, count))
                return
            self._wait(60.0 - (now - self._window[0][0]))


def _dispatch(
    provider: EmbeddingProvider,
    profile: EmbeddingProfile,
    ledger: PilotLedger,
    request_id: UUID,
    payload: tuple[tuple[int, ...], ...],
) -> EmbeddingReceipt:
    try:
        return provider.embed(profile, payload, request_id)
    except EmbeddingDispatchError as exc:
        ledger.failed(request_id, exc)
        raise


def _reserve(
    ledger: PilotLedger,
    rate: PilotRateLimit,
    request_id: UUID,
    kind: RequestKind,
    payload: tuple[tuple[int, ...], ...],
) -> None:
    count = token_count(payload)
    rate(count)
    ledger.reserve(request_id, kind, count)


def _store_batch(
    store: FomcEmbeddingStore,
    run: EmbeddingRun,
    batch: EmbeddingBatch,
    plan: PilotPlan,
    payload: tuple[tuple[int, ...], ...],
    ledger: PilotLedger,
    provider: EmbeddingProvider,
    rate: PilotRateLimit,
) -> None:
    request_id = batch.request_id
    try:
        _reserve(ledger, rate, request_id, "passages", payload)
    except EmbeddingError:
        store.record_failure(run, batch, "cancelled_before_dispatch")
        raise
    store.mark_dispatched(run, batch)
    try:
        receipt = _dispatch(provider, plan.profile, ledger, request_id, payload)
    except EmbeddingDispatchError as exc:
        store.record_failure(run, batch, exc.code, exc.evidence)
        raise
    ledger.received(request_id, receipt)
    vectors = receipt.vectors(plan.profile, len(batch.inputs))
    if store.publish(run, batch, receipt) != len(batch.inputs):
        raise EmbeddingError("embedding_publication_mismatch")
    ledger.complete(request_id, tuple(vectors[i].sha256 for i in range(len(vectors))))


def _compare(
    reader: Engine,
    plan: PilotPlan,
    query_vectors: dict[str, Sequence[float]],
) -> list[dict[str, object]]:
    queries = FomcQueries(reader)
    reports: list[dict[str, object]] = []
    for case_id, case in plan.cases:
        scope = case.scope.query_scope()
        expected = set(case.expected_passages)
        keyword = queries.search(
            SearchRequest(
                case.query,
                kind=scope.kind,
                meeting_end=scope.meeting_end,
                published_on=scope.published_on,
            )
        )
        vector = search_fomc_vector(
            reader, plan.profile.profile_id, query_vectors[case.query], scope
        )
        if case.type == "empty_scope":
            if vector.status != "no_documents_in_scope" or vector.results:
                raise EmbeddingError("pilot_scope_mismatch")
        elif vector.status != "ok":
            raise EmbeddingError("pilot_corpus_incomplete")
        hits: list[SearchHit | VectorSearchHit] = list(vector.results)
        hits.extend(keyword.results)
        for hit in hits:
            cite = hit.citation
            detail = queries.passage(cite.passage_id)
            if (
                not detail.is_current
                or detail.citation != cite
                or detail.text != hit.text
                or (scope.kind is not None and cite.kind != scope.kind)
                or (scope.meeting_end is not None and cite.meeting_end != scope.meeting_end)
                or (scope.published_on is not None and cite.published_on != scope.published_on)
            ):
                raise EmbeddingError("pilot_citation_mismatch")
        keyword_ids = [hit.citation.passage_id for hit in keyword.results]
        vector_ids = [hit.citation.passage_id for hit in vector.results]
        reports.append(
            {
                "case": case_id,
                "kind": case.type,
                "vector_status": vector.status,
                "keyword_status": keyword.status,
                "keyword_top_five": keyword_ids,
                "keyword_expected_rank": next(
                    (i for i, p in enumerate(keyword_ids, 1) if p in expected), None
                ),
                "vector_expected_rank": next(
                    (i for i, p in enumerate(vector_ids, 1) if p in expected), None
                ),
                "vector_top_five": [
                    {
                        "passage_id": hit.citation.passage_id,
                        "distance": hit.distance,
                        "citation_sha256": sha256(hit.citation.model_dump_json().encode("utf-8")),
                    }
                    for hit in vector.results
                ],
                "support_review": "manual_required"
                if case.type == "unsupported"
                else "citation_checked",
            }
        )
    return reports


def run_embedding_pilot(
    plan: PilotPlan,
    ledger: PilotLedger,
    provider: EmbeddingProvider,
    rate: PilotRateLimit,
    *,
    admin: Engine,
    writer: FomcEmbeddingStore,
    reader: Engine,
) -> list[dict[str, object]]:
    ledger.check_plan(plan)
    profile_id = register_embedding_profile(admin, plan.profile, must_create=True)
    run = writer.start_run(
        profile_id,
        PassageScope(),
        request_limit=len(plan.batches),
        expected_inputs=plan.inputs,
    )
    if run is None:
        raise EmbeddingError("pilot_run_unavailable")
    for expected, payload in zip(plan.batches, plan.passage_tokens, strict=True):
        batch = writer.reserve_next(run)
        if batch is None:
            raise EmbeddingError("embedding_manifest_changed")
        if batch.inputs != expected:
            writer.record_failure(run, batch, "cancelled_before_dispatch")
            raise EmbeddingError("embedding_manifest_changed")
        _store_batch(writer, run, batch, plan, payload, ledger, provider, rate)
    request_id = uuid4()
    _reserve(ledger, rate, request_id, "queries", plan.query_tokens)
    receipt = _dispatch(provider, plan.profile, ledger, request_id, plan.query_tokens)
    ledger.received(request_id, receipt)
    query_vectors = receipt.vectors(plan.profile, len(plan.queries))
    original = {output.position: output.coordinates for output in receipt.outputs}
    ledger.complete(request_id, tuple(query_vectors[i].sha256 for i in range(len(plan.queries))))
    reports = _compare(reader, plan, {query: original[i] for i, query in enumerate(plan.queries)})
    ledger.finish(reports)
    return reports
