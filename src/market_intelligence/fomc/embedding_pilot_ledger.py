"""Exclusive, conservative reservations for one approved manual pilot."""

import json
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from market_intelligence.fomc.embedding_pilot_plan import (
    PILOT_COST_LIMIT,
    PILOT_PRICE_PER_MILLION,
    PILOT_REQUEST_LIMIT,
    PILOT_TOKEN_LIMIT,
    PilotPlan,
)
from market_intelligence.fomc.embedding_provider import EmbeddingDispatchError
from market_intelligence.fomc.vector_models import EmbeddingError, EmbeddingReceipt

RequestKind = Literal["passages", "queries"]


def input_cost(tokens: int) -> Decimal:
    return Decimal(tokens) * Decimal(PILOT_PRICE_PER_MILLION) / Decimal(1_000_000)


class PilotLedger:
    def __init__(
        self,
        path: Path,
        plan: PilotPlan,
        *,
        attempt_limit: int,
        token_limit: int,
        approval_sha256: str,
    ) -> None:
        allocations = plan.allocations
        if (
            type(attempt_limit) is not int
            or type(token_limit) is not int
            or not len(allocations) <= attempt_limit <= PILOT_REQUEST_LIMIT
            or not sum(allocations) <= token_limit <= PILOT_TOKEN_LIMIT
            or input_cost(token_limit) > Decimal(PILOT_COST_LIMIT)
        ):
            raise EmbeddingError("pilot_spend_limit")
        self.path = path
        self._allocations = allocations
        self._profile_id = plan.profile.profile_id
        self._requests: list[dict[str, Any]] = []
        self._record: dict[str, Any] = {
            "state": "active",
            "approval_sha256": approval_sha256,
            "profile_id": self._profile_id,
            "attempt_limit": attempt_limit,
            "token_limit": token_limit,
            "price_per_million_usd": PILOT_PRICE_PER_MILLION,
            "planned_input_tokens": allocations,
            "requests": self._requests,
        }
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                self._write(handle)
        except OSError:
            raise EmbeddingError("pilot_ledger_unavailable_or_already_used") from None

    def _write(self, handle: Any) -> None:
        json.dump(self._record, handle, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    def _save(self) -> None:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                delete=False,
            ) as handle:
                self._write(handle)
                temporary = Path(handle.name)
            os.replace(temporary, self.path)
        except OSError:
            raise EmbeddingError("pilot_ledger_write_failed") from None

    def check_plan(self, plan: PilotPlan) -> None:
        if self._profile_id != plan.profile.profile_id or self._allocations != plan.allocations:
            raise EmbeddingError("pilot_ledger_plan_mismatch")

    def reserve(self, request_id: UUID, kind: RequestKind, tokens: int) -> None:
        index = len(self._requests)
        if (
            self._record["state"] != "active"
            or index >= len(self._allocations)
            or kind != ("queries" if index == len(self._allocations) - 1 else "passages")
            or tokens != self._allocations[index]
            or any(row["client_request_id"] == str(request_id) for row in self._requests)
            or (self._requests and self._requests[-1]["state"] != "complete")
        ):
            raise EmbeddingError("pilot_request_not_reserved")
        self._requests.append(
            {
                "number": index + 1,
                "kind": kind,
                "client_request_id": str(request_id),
                "state": "reserved",
                "reserved_at": datetime.now(UTC).isoformat(),
                "reserved_input_tokens": tokens,
                "reserved_usd": str(input_cost(tokens)),
            }
        )
        self._save()

    def received(self, request_id: UUID, receipt: EmbeddingReceipt) -> None:
        row = self._current(request_id, "reserved")
        if (
            receipt.prompt_tokens != row["reserved_input_tokens"]
            or receipt.total_tokens != receipt.prompt_tokens
        ):
            raise EmbeddingError("pilot_usage_mismatch")
        row.update(
            state="received",
            provider_request_id=receipt.request_id,
            returned_model=receipt.model,
            prompt_tokens=receipt.prompt_tokens,
            total_tokens=receipt.total_tokens,
        )
        self._save()

    def failed(self, request_id: UUID, error: EmbeddingDispatchError) -> None:
        row = self._current(request_id, "reserved")
        evidence = error.evidence
        row.update(
            state=error.code,
            provider_request_id=evidence.request_id,
            returned_model=evidence.model,
            prompt_tokens=evidence.prompt_tokens,
            total_tokens=evidence.total_tokens,
        )
        self._record["state"] = "review_required"
        self._save()

    def _current(self, request_id: UUID, previous: str) -> dict[str, Any]:
        if not self._requests or self._record["state"] != "active":
            raise EmbeddingError("pilot_request_not_reserved")
        row = self._requests[-1]
        if row["client_request_id"] != str(request_id) or row["state"] != previous:
            raise EmbeddingError("pilot_request_not_reserved")
        return row

    def complete(self, request_id: UUID, vector_hashes: tuple[str, ...]) -> None:
        self._current(request_id, "received").update(state="complete", vector_sha256=vector_hashes)
        self._save()

    def finish(self, reports: list[dict[str, object]]) -> None:
        if (
            self._record["state"] != "active"
            or len(self._requests) != len(self._allocations)
            or any(row["state"] != "complete" for row in self._requests)
        ):
            raise EmbeddingError("pilot_requests_unreconciled")
        self._record.update(
            state="retrieval_review_pending",
            cases=reports,
            total_tokens=sum(self._allocations),
            total_usd=str(input_cost(sum(self._allocations))),
        )
        self._save()
