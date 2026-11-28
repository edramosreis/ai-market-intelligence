import json
import os
from pathlib import Path
from uuid import UUID

import httpx2 as httpx
import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy import Engine

from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.embedding_pilot import PilotRateLimit, run_embedding_pilot
from market_intelligence.fomc.embedding_pilot_ledger import PilotLedger
from market_intelligence.fomc.embedding_pilot_plan import PilotPlan, PilotPreview, build_pilot_plan
from market_intelligence.fomc.embedding_provider import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EmbeddingDispatchError,
    OpenAIEmbeddingProvider,
)
from market_intelligence.fomc.retrieval import (
    PassageScope,
    current_policy_passages,
    retrieval_snapshot,
)
from market_intelligence.fomc.vector_models import EmbeddingError
from tests.integration.test_fomc_embeddings import VectorCase, vector_case  # noqa: F401
from tests.integration.test_fomc_retrieval import fomc_reader  # noqa: F401
from tests.integration.test_fomc_store import store  # noqa: F401
from tests.unit.test_fomc_embedding_pilot import BytesEncoding

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("RUN_VECTOR_TESTS") != "1", reason="Requires pgvector"),
]


def make_plan(reader: Engine) -> PilotPlan:
    with retrieval_snapshot(reader) as conn:
        coverage, query = current_policy_passages(conn, PassageScope())
        items = sorted(
            (row.passage_id, row.text_sha256, len(row.body.encode("utf-8")))
            for row in conn.execute(query)
        )
    unfiltered = {"kind": None, "meeting_end": None, "published_on": None}
    case = {
        "query": "interest rates",
        "scope": unfiltered,
        "type": "literal",
        "expected_passages": [items[0][0]],
    }
    preview = {
        "eligible": items,
        "confirmed_ready_documents": coverage.ready,
        "by_case": {
            "policy": case,
            "no_docs": case
            | {
                "scope": unfiltered | {"meeting_end": "2000-01-01"},
                "type": "empty_scope",
                "expected_passages": [],
            },
        },
    }
    return build_pilot_plan(
        reader, PilotPreview.model_validate_json(json.dumps(preview)), BytesEncoding(), UUID(int=22)
    )


def ledger_for(path: Path, plan: PilotPlan) -> PilotLedger:
    return PilotLedger(path, plan, attempt_limit=13, token_limit=500_000, approval_sha256="a" * 64)


def test_pilot_runs_through_store_and_exact_citation_readers(
    vector_case: VectorCase,  # noqa: F811
    fomc_reader: Engine,  # noqa: F811
    tmp_path: Path,
) -> None:
    vector_case.collect(PILOT_DOCUMENTS[0])
    plan = make_plan(fomc_reader)
    path = tmp_path / "pilot.json"
    ledger = ledger_for(path, plan)
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        for engine in (vector_case.admin, vector_case.embeddings.engine, fomc_reader):
            assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        stored = json.loads(path.read_text())["requests"][-1]
        assert stored["state"] == "reserved"
        assert stored["client_request_id"] == request.headers["x-client-request-id"]
        calls.append(stored["kind"])
        inputs = json.loads(request.content)["input"]
        count = sum(map(len, inputs))
        assert count == stored["reserved_input_tokens"]
        return httpx.Response(
            200,
            headers={"x-request-id": f"req_{len(calls)}"},
            json={
                "object": "list",
                "model": EMBEDDING_MODEL,
                "usage": {"prompt_tokens": count, "total_tokens": count},
                "data": [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
                    }
                    for index in reversed(range(len(inputs)))
                ],
            },
        )

    with OpenAIEmbeddingProvider(SecretStr("test"), transport=httpx.MockTransport(respond)) as sdk:
        cases = run_embedding_pilot(
            plan,
            ledger,
            sdk,
            PilotRateLimit(13, 500_000),
            admin=vector_case.admin,
            writer=vector_case.embeddings,
            reader=fomc_reader,
        )
    assert calls == ["passages", "queries"]
    assert cases[0]["vector_expected_rank"] == 1
    assert cases[1]["vector_status"] == "no_documents_in_scope"
    private = json.loads(path.read_text())
    assert private["state"] == "retrieval_review_pending"
    assert private["total_tokens"] == sum(plan.allocations)
    assert all(row["vector_sha256"] for row in private["requests"])
    assert "interest rates" not in path.read_text()
    with pytest.raises(EmbeddingError, match="pilot_ledger_unavailable_or_already_used"):
        ledger_for(path, plan)


def test_timeout_stops_before_queries_and_retains_usage_reservation(
    vector_case: VectorCase,  # noqa: F811
    fomc_reader: Engine,  # noqa: F811
    tmp_path: Path,
) -> None:
    vector_case.collect(PILOT_DOCUMENTS[0])
    plan = make_plan(fomc_reader)
    path = tmp_path / "interrupted.json"
    with pytest.raises(EmbeddingError, match="pilot_spend_limit"):
        PilotLedger(path, plan, attempt_limit=13, token_limit=1, approval_sha256="a" * 64)
    assert not path.exists()
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        raise httpx.ReadTimeout("synthetic")

    with (
        OpenAIEmbeddingProvider(SecretStr("test"), transport=httpx.MockTransport(respond)) as sdk,
        pytest.raises(EmbeddingDispatchError, match="dispatch_uncertain"),
    ):
        run_embedding_pilot(
            plan,
            ledger_for(path, plan),
            sdk,
            PilotRateLimit(13, 500_000),
            admin=vector_case.admin,
            writer=vector_case.embeddings,
            reader=fomc_reader,
        )
    assert len(calls) == 1
    evidence = json.loads(path.read_text())
    assert evidence["state"] == "review_required"
    assert evidence["requests"][0]["reserved_input_tokens"] == plan.allocations[0]
    assert evidence["requests"][0]["total_tokens"] is None
    with vector_case.admin.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT state FROM fomc_embedding_runs"))
            == "reconciliation_required"
        )
        assert connection.scalar(sa.text("SELECT count(*) FROM fomc_passage_embeddings")) == 0


def test_changed_corpus_refuses_before_any_transport_call(
    vector_case: VectorCase,  # noqa: F811
    fomc_reader: Engine,  # noqa: F811
    tmp_path: Path,
) -> None:
    vector_case.collect(PILOT_DOCUMENTS[0])
    plan = make_plan(fomc_reader)
    vector_case.collect(PILOT_DOCUMENTS[1])
    path = tmp_path / "changed.json"

    def respond(_: httpx.Request) -> httpx.Response:
        pytest.fail("Manifest drift reached the transport")

    with (
        OpenAIEmbeddingProvider(SecretStr("test"), transport=httpx.MockTransport(respond)) as sdk,
        pytest.raises(EmbeddingError, match="embedding_manifest_changed"),
    ):
        run_embedding_pilot(
            plan,
            ledger_for(path, plan),
            sdk,
            PilotRateLimit(13, 500_000),
            admin=vector_case.admin,
            writer=vector_case.embeddings,
            reader=fomc_reader,
        )
    assert json.loads(path.read_text())["requests"] == []
    with vector_case.admin.connect() as connection:
        assert connection.scalar(sa.text("SELECT count(*) FROM fomc_embedding_runs")) == 0
