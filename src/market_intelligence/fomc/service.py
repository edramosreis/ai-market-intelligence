"""Manual four-document acquisition; no database connection spans HTTP or file work."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.db.fomc_store import FomcStore, FomcStoreError
from market_intelligence.fomc.artifacts import ArtifactError, ArtifactStore
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.client import AcquisitionError, FomcAcquisitionClient
from market_intelligence.fomc.validation import (
    FomcValidationError,
    validate_calendar,
    validate_document,
)
from market_intelligence.time_utils import utc_now


@dataclass(frozen=True)
class IngestionReport:
    run_id: UUID
    documents_completed: int
    new_versions: int
    http_attempts: int
    wire_bytes: int
    entity_bytes: int


class FomcIngestionError(RuntimeError):
    def __init__(
        self,
        code: str,
        run_id: UUID | None,
        audit_recorded: bool,
        unreferenced_artifact_possible: bool,
    ) -> None:
        self.code, self.run_id = code, run_id
        self.audit_recorded = audit_recorded
        self.unreferenced_artifact_possible = unreferenced_artifact_possible
        super().__init__(code)


def ingest_pilot(
    store: FomcStore,
    client: FomcAcquisitionClient,
    artifacts: ArtifactStore,
    *,
    now: Callable[[], datetime] = utc_now,
) -> IngestionReport:
    run_id: UUID | None = None
    orphan_possible = False
    completed, new_versions = 0, 0

    def stats() -> dict[str, int]:
        return dict(
            http_attempts=client.requests_used,
            wire_bytes=client.wire_bytes,
            entity_bytes=client.entity_bytes,
        )

    try:
        run_id = store.reserve(now())
        calendar = client.fetch_calendar()
        evidence = validate_calendar(calendar.body, calendar.content_type)
        orphan_possible = True
        artifacts.put(calendar.body)
        store.record_calendar(run_id, calendar, evidence, artifacts, stats())
        orphan_possible = False
        for spec in PILOT_DOCUMENTS:
            fetched = client.fetch(spec.document_id)
            validated = validate_document(fetched, evidence)
            orphan_possible = True
            artifacts.put(fetched.body)
            new_versions += store.record_document(
                run_id, validated, calendar.receipt_id, artifacts, stats()
            )
            orphan_possible = False
            completed += 1
        store.finish(run_id, now(), stats())
        return IngestionReport(
            run_id,
            completed,
            new_versions,
            client.requests_used,
            client.wire_bytes,
            client.entity_bytes,
        )
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, AcquisitionError):
            code = error.code.value
        elif isinstance(error, FomcValidationError):
            code = "invalid_fomc_document"
        elif isinstance(error, ArtifactError):
            code = "artifact_error"
        elif isinstance(error, SQLAlchemyError):
            code = "database_error"
        elif isinstance(error, KeyboardInterrupt):
            code = "interrupted"
        elif isinstance(error, FomcStoreError):
            code = (
                error.code
                if error.code in {"stale_receipt", "running_command", "catalog_mismatch"}
                else "internal_error"
            )
        else:
            code = "internal_error"
        recorded = False
        if run_id is not None:
            try:
                store.finish(run_id, now(), stats(), code)
                recorded = True
            except SQLAlchemyError, ValueError, FomcStoreError:
                pass  # Explicitly reported; the durable running reservation must be inspected.
        raise FomcIngestionError(code, run_id, recorded, orphan_possible) from None
