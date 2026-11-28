"""Load the approved manifest and prepare exact token batches without a provider call."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import Engine

from market_intelligence.fomc.embedding_provider import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    MAX_INPUT_TOKENS,
    token_count,
)
from market_intelligence.fomc.embedding_tokenizer import (
    TOKENIZER,
    TOKENIZER_REVISION,
    OrdinaryEncoding,
    encode_reviewed_text,
)
from market_intelligence.fomc.models import DocumentKind
from market_intelligence.fomc.retrieval import (
    PassageScope,
    current_policy_passages,
    retrieval_snapshot,
)
from market_intelligence.fomc.vector_db import check_vector_connection, require_vector_target
from market_intelligence.fomc.vector_models import (
    MAX_BATCH_INPUTS,
    MAX_RUN_INPUTS,
    EmbeddingError,
    EmbeddingInput,
    EmbeddingProfile,
    sha256,
)

PILOT_REQUEST_LIMIT = 13
PILOT_TOKEN_LIMIT = 500_000
PILOT_PRICE_PER_MILLION = "0.02"
PILOT_COST_LIMIT = "0.01"

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
CaseKind = Literal["literal", "date_filtered", "paraphrase", "empty_scope", "unsupported"]


class CaseScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    kind: DocumentKind | None
    meeting_end: date | None
    published_on: date | None

    def query_scope(self) -> PassageScope:
        return PassageScope(self.kind, self.meeting_end, self.published_on)


class PilotCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    query: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    scope: CaseScope
    type: CaseKind
    expected_passages: Annotated[tuple[Digest, ...], Field(max_length=5)]


class PilotPreview(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    eligible: Annotated[
        tuple[tuple[Digest, Digest, int], ...], Field(min_length=1, max_length=MAX_RUN_INPUTS)
    ]
    by_case: Annotated[dict[str, PilotCase], Field(min_length=1, max_length=12)]
    confirmed_ready_documents: Annotated[int, Field(ge=1, le=16)]


def read_hashed_file(path: Path, expected: str, max_bytes: int = 256 * 1024) -> bytes:
    if path.is_symlink():
        raise EmbeddingError("pilot_file_unavailable")
    try:
        with path.open("rb") as handle:
            content = handle.read(max_bytes + 1)
    except OSError:
        raise EmbeddingError("pilot_file_unavailable") from None
    if len(content) > max_bytes or sha256(content) != expected:
        raise EmbeddingError("pilot_file_hash_mismatch")
    return content


@dataclass(frozen=True)
class PilotPlan:
    profile: EmbeddingProfile
    batches: tuple[tuple[EmbeddingInput, ...], ...]
    passage_tokens: tuple[tuple[tuple[int, ...], ...], ...]
    cases: tuple[tuple[str, PilotCase], ...]
    queries: tuple[str, ...]
    query_tokens: tuple[tuple[int, ...], ...]

    @property
    def inputs(self) -> tuple[EmbeddingInput, ...]:
        return tuple(item for batch in self.batches for item in batch)

    @property
    def allocations(self) -> tuple[int, ...]:
        return tuple(token_count(batch) for batch in (*self.passage_tokens, self.query_tokens))


def build_pilot_plan(
    engine: Engine, preview: PilotPreview, encoding: OrdinaryEncoding, epoch: UUID
) -> PilotPlan:
    require_vector_target(engine, "reader")
    profile = EmbeddingProfile(
        "openai", EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, TOKENIZER, TOKENIZER_REVISION, epoch
    )
    with retrieval_snapshot(engine) as conn:
        check_vector_connection(conn, engine)
        coverage, query = current_policy_passages(conn, PassageScope())
        rows = sorted(
            conn.execute(query.limit(MAX_RUN_INPUTS + 1)).all(), key=lambda row: row.passage_id
        )
    measured = tuple(
        (row.passage_id, row.text_sha256, len(row.body.encode("utf-8"))) for row in rows
    )
    if (
        measured != preview.eligible
        or coverage.ready != preview.confirmed_ready_documents
        or len({row.passage_id for row in rows}) != len(rows)
        or any(sha256(row.body.encode("utf-8")) != row.text_sha256 for row in rows)
    ):
        raise EmbeddingError("embedding_manifest_changed")
    all_ids = {row.passage_id for row in rows}
    for case in preview.by_case.values():
        if not set(case.expected_passages) <= all_ids or bool(case.expected_passages) == (
            case.type in {"empty_scope", "unsupported"}
        ):
            raise EmbeddingError("invalid_pilot_cases")
    batches = tuple(
        tuple(
            EmbeddingInput(row.passage_id, row.text_sha256)
            for row in rows[i : i + MAX_BATCH_INPUTS]
        )
        for i in range(0, len(rows), MAX_BATCH_INPUTS)
    )
    payloads = tuple(
        tuple(encode_reviewed_text(row.body, encoding) for row in rows[i : i + MAX_BATCH_INPUTS])
        for i in range(0, len(rows), MAX_BATCH_INPUTS)
    )
    queries = tuple(dict.fromkeys(case.query for case in preview.by_case.values()))
    query_tokens = tuple(encode_reviewed_text(query_text, encoding) for query_text in queries)
    if any(len(tokens) > min(512, MAX_INPUT_TOKENS) for tokens in query_tokens):
        raise EmbeddingError("pilot_query_token_limit")
    plan = PilotPlan(
        profile, batches, payloads, tuple(preview.by_case.items()), queries, query_tokens
    )
    allocations = plan.allocations
    if len(allocations) > PILOT_REQUEST_LIMIT or sum(allocations) > PILOT_TOKEN_LIMIT:
        raise EmbeddingError("pilot_spend_limit")
    return plan
