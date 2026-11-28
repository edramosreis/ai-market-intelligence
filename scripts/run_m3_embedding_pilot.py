"""One-shot host entry point for the separately authorized FOMC embedding pilot."""

import argparse
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from m3 import check_worktree, read_settings
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.fomc_embedding_store import FomcEmbeddingStore
from market_intelligence.fomc.embedding_pilot import PilotRateLimit, run_embedding_pilot
from market_intelligence.fomc.embedding_pilot_ledger import PilotLedger
from market_intelligence.fomc.embedding_pilot_plan import (
    PILOT_REQUEST_LIMIT,
    PILOT_TOKEN_LIMIT,
    Digest,
    PilotPreview,
    build_pilot_plan,
    read_hashed_file,
)
from market_intelligence.fomc.embedding_provider import OpenAIEmbeddingProvider
from market_intelligence.fomc.embedding_tokenizer import load_pilot_tokenizer
from market_intelligence.fomc.retrieval_models import RetrievalError
from market_intelligence.fomc.vector_models import EmbeddingError


class Approval(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    code_head: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    preview_sha256: Digest
    profile_epoch: UUID
    project_id: Annotated[str, StringConstraints(pattern=r"^proj_[A-Za-z0-9_-]{1,100}$")]
    price_per_million_usd: Literal["0.02"]
    attempt_limit: Literal[13]
    token_limit: Annotated[int, Field(ge=1, le=PILOT_TOKEN_LIMIT)]
    allocations: Annotated[
        tuple[Annotated[int, Field(ge=1, le=300_000)], ...],
        Field(min_length=PILOT_REQUEST_LIMIT, max_length=PILOT_REQUEST_LIMIT),
    ]
    requests_per_minute: Annotated[int, Field(ge=1, le=10_000)]
    tokens_per_minute: Annotated[int, Field(ge=1, le=10_000_000)]


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=10, check=True
    )
    return result.stdout.strip()


def _private_file(parent: Path, path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_relative_to(parent) or not resolved.is_file():
        raise EmbeddingError("pilot_private_file_required")
    return resolved


def _run(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parents[1]
    check_worktree(root)
    common_git = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    private = (common_git.parent / ".private").resolve(strict=True)
    approval_file = _private_file(private, args.approval)
    preview_file = _private_file(private, args.preview)
    vocabulary_file = _private_file(private, args.vocabulary)
    approval = Approval.model_validate_json(
        read_hashed_file(approval_file, args.approval_sha256, max_bytes=8192)
    )
    if _git(root, "rev-parse", "HEAD") != approval.code_head or _git(root, "status", "--porcelain"):
        raise EmbeddingError("pilot_checkout_mismatch")
    preview = PilotPreview.model_validate_json(
        read_hashed_file(preview_file, approval.preview_sha256)
    )
    if (preview.confirmed_ready_documents, len(preview.eligible), len(preview.by_case)) != (
        16,
        376,
        8,
    ):
        raise EmbeddingError("pilot_scope_mismatch")
    if len({case.query for case in preview.by_case.values()}) != 7:
        raise EmbeddingError("pilot_scope_mismatch")
    if "OPENAI_ORG_ID" in os.environ or "RUN_POSTGRES_TESTS" in os.environ:
        raise EmbeddingError("pilot_environment_mismatch")
    key = os.environ.get("M3_EMBEDDING_API_KEY")
    if not key:
        raise EmbeddingError("embedding_key_unavailable")
    settings = read_settings(root, False)
    database = DatabaseSettings(
        _env_file=None,  # type: ignore[call-arg]
        db="market_intelligence_m3",
        host="127.0.0.1",
        port=int(settings["M3_POSTGRES_PORT"]),
        admin_user="m3_admin",
        admin_password=SecretStr(settings["M3_ADMIN_PASSWORD"]),
        ingest_user="m3_ingest",
        ingest_password=SecretStr(settings["M3_INGEST_PASSWORD"]),
        read_user="m3_reader",
        read_password=SecretStr(settings["M3_READ_PASSWORD"]),
    )
    admin = create_db_engine(database, DatabaseRole.ADMIN)
    ingest = create_db_engine(database, DatabaseRole.INGEST)
    reader = create_db_engine(database, DatabaseRole.READ)
    try:
        plan = build_pilot_plan(
            reader, preview, load_pilot_tokenizer(vocabulary_file), approval.profile_epoch
        )
        if (
            approval.allocations != plan.allocations
            or max(plan.allocations) > approval.tokens_per_minute
        ):
            raise EmbeddingError("pilot_approved_allocations_mismatch")
        # Exclusive creation: an existing record always requires human reconciliation or review.
        path = approval_file.parent / f"fomc-embedding-pilot-{approval.profile_epoch.hex}.json"
        with OpenAIEmbeddingProvider(SecretStr(key), project=approval.project_id) as provider:
            ledger = PilotLedger(
                path,
                plan,
                attempt_limit=approval.attempt_limit,
                token_limit=approval.token_limit,
                approval_sha256=args.approval_sha256,
            )
            run_embedding_pilot(
                plan,
                ledger,
                provider,
                PilotRateLimit(approval.requests_per_minute, approval.tokens_per_minute),
                admin=admin,
                writer=FomcEmbeddingStore(ingest),
                reader=reader,
            )
        print(
            "Pilot requests recorded. Inspect the private retrieval evidence and usage for review."
        )
    finally:
        for engine in (admin, ingest, reader):
            engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--approval-sha256", required=True)
    parser.add_argument("--preview", required=True, type=Path)
    parser.add_argument("--vocabulary", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        _run(args)
    except (EmbeddingError, RetrievalError) as error:
        print(f"Pilot stopped ({error}). Review private evidence before any further dispatch.")
        return 1
    except OSError, ValueError, SQLAlchemyError, subprocess.SubprocessError, KeyError:
        print("Pilot stopped during configuration or database access. Inspect private evidence.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
