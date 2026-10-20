"""Short transactions for original receipts, replay provenance and durable run reservation."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert

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
    fomc_ingestion_runs as runs,
)
from market_intelligence.db.fomc_tables import (
    fomc_raw_versions as versions,
)
from market_intelligence.fomc.artifacts import ArtifactStore
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.client import FetchedCalendar, FetchedDocument
from market_intelligence.fomc.isolation import require_fomc_database
from market_intelligence.fomc.models import CALENDAR_URL
from market_intelligence.fomc.validation import (
    VALIDATOR_VERSION,
    CalendarEvidence,
    ValidatedDocument,
)
from market_intelligence.time_utils import as_utc

LOCK = 360006


class FomcStoreError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FomcStore:
    def __init__(self, engine: Engine) -> None:
        require_fomc_database(engine.url)
        self.engine = engine

    def reserve(self, at: datetime) -> UUID:
        run_id = uuid4()
        with self.engine.begin() as conn:
            conn.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK})
            if conn.execute(sa.select(runs.c.run_id).where(runs.c.status == "running")).first():
                raise FomcStoreError("running_command")
            catalog = conn.execute(sa.select(documents)).mappings().all()
            expected = {d.document_id: d for d in PILOT_DOCUMENTS}
            if set(row["document_id"] for row in catalog) != set(expected):
                raise FomcStoreError("catalog_mismatch")
            for row in catalog:
                spec = expected[row["document_id"]]
                if any(
                    row[name] != value
                    for name, value in {
                        "kind": spec.kind.value,
                        "meeting_start": spec.meeting_start,
                        "meeting_end": spec.meeting_end,
                        "published_on": spec.published_on,
                        "official_url": spec.url,
                        "publication_evidence_url": spec.publication_evidence_url,
                        "publication_precision": "date",
                        "explicit_release_time_text": spec.release_time_text,
                    }.items()
                ):
                    raise FomcStoreError("catalog_mismatch")
            conn.execute(
                runs.insert().values(
                    run_id=run_id,
                    started_at=as_utc(at),
                    status="running",
                    requested_ids=list(expected),
                )
            )
        return run_id

    @staticmethod
    def _receipt(
        fetched: FetchedCalendar | FetchedDocument, run_id: UUID, url: str
    ) -> dict[str, Any]:
        return dict(
            receipt_id=fetched.receipt_id,
            run_id=run_id,
            requested_url=url,
            final_url=url,
            http_status=200,
            content_type=fetched.content_type,
            content_encoding=fetched.content_encoding,
            wire_bytes=fetched.encoded_size,
            entity_bytes=len(fetched.body),
            raw_sha256=fetched.raw_sha256,
            fetch_started_at=fetched.fetch_started_at,
            received_at=fetched.received_at,
            etag=fetched.etag,
            last_modified=fetched.last_modified,
            validator_version=VALIDATOR_VERSION,
        )

    def record_calendar(
        self,
        run_id: UUID,
        fetched: FetchedCalendar,
        evidence: CalendarEvidence,
        artifacts: ArtifactStore,
        stats: dict[str, int],
    ) -> None:
        artifacts.verify(fetched.raw_sha256, len(fetched.body))
        if fetched.raw_sha256 != evidence.raw_sha256:
            raise ValueError("Calendar evidence does not match the original")
        with self.engine.begin() as conn:
            self._running(conn, run_id)
            conn.execute(
                receipts.insert().values(
                    **self._receipt(fetched, run_id, CALENDAR_URL),
                    resource_kind="calendar",
                    verified_title="FOMC meeting calendars",
                )
            )
            conn.execute(runs.update().where(runs.c.run_id == run_id).values(**stats))

    @staticmethod
    def _running(conn: sa.Connection, run_id: UUID) -> None:
        row = (
            conn.execute(sa.select(runs).where(runs.c.run_id == run_id).with_for_update())
            .mappings()
            .one_or_none()
        )
        if row is None or row["status"] != "running":
            raise FomcStoreError("run_not_running")

    def record_document(
        self,
        run_id: UUID,
        validated: ValidatedDocument,
        calendar_id: UUID,
        artifacts: ArtifactStore,
        stats: dict[str, int],
    ) -> bool:
        fetched = validated.fetched
        artifacts.verify(fetched.raw_sha256, len(fetched.body))
        doc = fetched.document.document_id
        with self.engine.begin() as conn:
            self._running(conn, run_id)
            calendar = (
                conn.execute(
                    sa.select(receipts).where(
                        receipts.c.receipt_id == calendar_id,
                        receipts.c.run_id == run_id,
                        receipts.c.resource_kind == "calendar",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if calendar is None or calendar["raw_sha256"] != validated.calendar_sha256:
                raise ValueError("Missing matching calendar evidence")
            if conn.execute(
                sa.select(receipts.c.receipt_id).where(
                    receipts.c.run_id == run_id, receipts.c.document_id == doc
                )
            ).first():
                raise FomcStoreError("duplicate_document")
            previous = (
                conn.execute(
                    sa.select(current).where(current.c.document_id == doc).with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if previous is not None and (
                fetched.fetch_started_at < previous["last_fetch_started_at"]
                or fetched.received_at < previous["received_at"]
                or (
                    fetched.fetch_started_at == previous["last_fetch_started_at"]
                    and fetched.raw_version_id != previous["raw_version_id"]
                )
            ):
                raise FomcStoreError("stale_receipt")
            added = (
                conn.execute(
                    insert(versions)
                    .values(
                        raw_version_id=fetched.raw_version_id,
                        document_id=doc,
                        raw_sha256=fetched.raw_sha256,
                        byte_count=len(fetched.body),
                        first_receipt_id=fetched.receipt_id,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[versions.c.document_id, versions.c.raw_sha256]
                    )
                    .returning(versions.c.raw_version_id)
                ).scalar_one_or_none()
                is not None
            )
            conn.execute(
                receipts.insert().values(
                    **self._receipt(fetched, run_id, fetched.document.url),
                    resource_kind="document",
                    document_id=doc,
                    raw_version_id=fetched.raw_version_id,
                    verified_title=validated.title,
                    meeting_label=validated.meeting_label,
                    publication_label=validated.publication_label,
                    release_time_text=validated.release_time_text,
                    page_update_text=validated.page_update_text,
                    calendar_receipt_id=calendar_id,
                )
            )
            values = dict(
                document_id=doc,
                raw_version_id=fetched.raw_version_id,
                last_receipt_id=fetched.receipt_id,
                last_fetch_started_at=fetched.fetch_started_at,
                received_at=fetched.received_at,
            )
            conn.execute(
                insert(current)
                .values(**values)
                .on_conflict_do_update(index_elements=[current.c.document_id], set_=values)
            )
            conn.execute(
                runs.update()
                .where(runs.c.run_id == run_id)
                .values(**stats, documents_completed=runs.c.documents_completed + 1)
            )
        return added

    def finish(
        self, run_id: UUID, at: datetime, stats: dict[str, int], error: str | None = None
    ) -> None:
        with self.engine.begin() as conn:
            self._running(conn, run_id)
            conn.execute(
                runs.update()
                .where(runs.c.run_id == run_id)
                .values(
                    **stats,
                    status="failed" if error else "succeeded",
                    finished_at=as_utc(at),
                    error_code=error,
                )
            )

    def reconcile(self, run_id: UUID, at: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK})
            self._running(conn, run_id)
            conn.execute(
                runs.update()
                .where(runs.c.run_id == run_id)
                .values(status="failed", finished_at=as_utc(at), error_code="interrupted")
            )

    def status(self) -> list[dict[str, Any]]:
        with (
            self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
            conn.begin(),
        ):
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            rows = conn.execute(
                sa.select(runs).order_by(runs.c.started_at.desc(), runs.c.run_id.desc()).limit(20)
            ).mappings()
            return [dict(row) for row in rows]
