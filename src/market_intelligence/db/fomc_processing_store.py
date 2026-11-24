"""Atomic FOMC processing publication and read-only status."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.sql.elements import ColumnElement

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
from market_intelligence.fomc.catalog import CATALOG_IDS, PILOT_DOCUMENTS, PILOT_IDS
from market_intelligence.fomc.chunking import ChunkerConfig
from market_intelligence.fomc.models import DocumentSpec
from market_intelligence.fomc.passage_models import ParserConfig
from market_intelligence.fomc.processing_models import (
    Outcome,
    ProcessingError,
    ProcessingInput,
    ProcessingOutput,
    ProcessingResult,
    Row,
)


def _read_only(conn: Connection) -> None:
    conn.exec_driver_sql("SET TRANSACTION READ ONLY")


def _check_rows(
    conn: Connection,
    table: sa.Table,
    expected: Sequence[Row],
    condition: ColumnElement[bool],
    *order: ColumnElement[Any],
) -> None:
    actual = (
        conn.execute(
            sa.select(*(table.c[key] for key in expected[0]))
            .where(condition)
            .order_by(*order)
            .limit(len(expected) + 1)
        )
        .mappings()
        .all()
    )
    if [dict(row) for row in actual] != list(expected):
        raise ProcessingError("output_conflict")


def processing_status(
    conn: Connection,
    parser: ParserConfig,
    chunker: ChunkerConfig,
    document_ids: tuple[str, ...] = PILOT_IDS,
) -> list[Row]:
    """Read document readiness within the caller's existing snapshot."""
    parser_hash, chunker_hash = parser.config_hash, chunker.config_hash
    created = attempts.alias("created")
    last = (
        sa.select(attempts.c.outcome, attempts.c.error_code, attempts.c.finished_at)
        .where(
            attempts.c.raw_version_id == current.c.raw_version_id,
            attempts.c.parser_config_sha256 == parser_hash,
            attempts.c.chunker_config_sha256 == chunker_hash,
            attempts.c.outcome != "stale",
        )
        .order_by(attempts.c.finished_at.desc(), attempts.c.attempt_id.desc())
        .limit(1)
        .lateral("latest")
    )
    ready = created.c.attempt_id.is_not(None)
    rows = conn.execute(
        sa.select(
            documents.c.document_id,
            documents.c.kind,
            documents.c.meeting_end,
            current.c.raw_version_id,
            current.c.received_at,
            sa.case(
                (current.c.raw_version_id.is_(None), "not_collected"),
                (ready, "ready"),
                (last.c.outcome == "failed", "failed"),
                else_="pending",
            ).label("state"),
            sa.case((ready, sets.c.passage_set_id)).label("passage_set_id"),
            sa.case((ready, generations.c.block_count), else_=0).label("block_count"),
            sa.case((ready, sets.c.passage_count), else_=0).label("passage_count"),
            created.c.finished_at.label("ready_at"),
            last.c.outcome.label("last_outcome"),
            last.c.error_code.label("last_error_code"),
            last.c.finished_at.label("last_finished_at"),
        )
        .select_from(
            documents.outerjoin(current, documents.c.document_id == current.c.document_id)
            .outerjoin(
                generations,
                sa.and_(
                    generations.c.raw_version_id == current.c.raw_version_id,
                    generations.c.parser_config_sha256 == parser_hash,
                ),
            )
            .outerjoin(
                sets,
                sa.and_(
                    sets.c.generation_id == generations.c.generation_id,
                    sets.c.chunker_config_sha256 == chunker_hash,
                ),
            )
            .outerjoin(
                created,
                sa.and_(
                    created.c.passage_set_id == sets.c.passage_set_id,
                    created.c.outcome == "created",
                ),
            )
            .outerjoin(last, sa.true())
        )
        .where(documents.c.document_id.in_(document_ids))
    ).mappings()
    by_id = {row["document_id"]: dict(row) for row in rows}
    return [by_id[document_id] for document_id in document_ids]


class FomcProcessingStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def inputs(
        self, documents: tuple[DocumentSpec, ...] = PILOT_DOCUMENTS
    ) -> dict[str, ProcessingInput]:
        calendar = receipts.alias("supporting_calendar")
        with (
            self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
            conn.begin(),
        ):
            _read_only(conn)
            rows = conn.execute(
                sa.select(
                    current.c.document_id,
                    versions.c.raw_version_id,
                    versions.c.raw_sha256,
                    versions.c.byte_count,
                    receipts.c.receipt_id,
                    receipts.c.fetch_started_at,
                    receipts.c.received_at,
                    receipts.c.content_type,
                    receipts.c.content_encoding,
                    receipts.c.wire_bytes,
                    receipts.c.etag,
                    receipts.c.last_modified,
                    receipts.c.final_url,
                    calendar.c.raw_sha256.label("calendar_sha256"),
                    calendar.c.entity_bytes.label("calendar_bytes"),
                    calendar.c.content_type.label("calendar_content_type"),
                )
                .select_from(
                    current.join(versions, current.c.raw_version_id == versions.c.raw_version_id)
                    .join(receipts, versions.c.first_receipt_id == receipts.c.receipt_id)
                    .join(calendar, receipts.c.calendar_receipt_id == calendar.c.receipt_id)
                )
                .where(current.c.document_id.in_(spec.document_id for spec in documents))
            ).mappings()
            by_id = {row["document_id"]: row for row in rows}
        result = {}
        for spec in documents:
            row = by_id.get(spec.document_id)
            if row is None:
                continue
            values = dict(row)
            values.pop("document_id")
            if values.pop("final_url") != spec.url:
                raise ProcessingError("identity_mismatch")
            result[spec.document_id] = ProcessingInput(spec, **values)
        return result

    def status(self, parser: ParserConfig, chunker: ChunkerConfig) -> list[Row]:
        with (
            self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn,
            conn.begin(),
        ):
            _read_only(conn)
            return processing_status(conn, parser, chunker, CATALOG_IDS)

    @staticmethod
    def _lock_current(conn: Connection, source: ProcessingInput) -> bool:
        raw_version = conn.scalar(
            sa.select(current.c.raw_version_id)
            .where(current.c.document_id == source.spec.document_id)
            .with_for_update()
        )
        return raw_version == source.raw_version_id

    @staticmethod
    def _write_audit(
        conn: Connection,
        run_id: UUID,
        source: ProcessingInput,
        started_at: datetime,
        parser_hash: str,
        chunker_hash: str,
        outcome: Outcome,
        output: ProcessingOutput | None = None,
        error: str | None = None,
    ) -> ProcessingResult:
        generation = output.generation if output else {}
        passage_set = output.passage_set if output else {}
        set_id = passage_set.get("passage_set_id")
        conn.execute(
            attempts.insert().values(
                attempt_id=uuid4(),
                run_id=run_id,
                document_id=source.spec.document_id,
                raw_version_id=source.raw_version_id,
                raw_sha256=source.raw_sha256,
                raw_byte_count=source.byte_count,
                parser_config_sha256=parser_hash,
                chunker_config_sha256=chunker_hash,
                started_at=started_at,
                outcome=outcome,
                error_code=error,
                generation_id=generation.get("generation_id"),
                passage_set_id=set_id,
            )
        )
        return ProcessingResult(
            source.spec.document_id,
            outcome,
            source.raw_version_id,
            set_id,
            generation.get("block_count", 0),
            passage_set.get("passage_count", 0),
            error,
            True,
        )

    def failure(
        self,
        run_id: UUID,
        source: ProcessingInput,
        started_at: datetime,
        parser: ParserConfig,
        chunker: ChunkerConfig,
        error: str,
    ) -> ProcessingResult:
        parser_hash, chunker_hash = parser.config_hash, chunker.config_hash
        with self.engine.begin() as conn:
            if not self._lock_current(conn, source):
                return self._write_audit(
                    conn,
                    run_id,
                    source,
                    started_at,
                    parser_hash,
                    chunker_hash,
                    "stale",
                    error="stale_raw_version",
                )
            return self._write_audit(
                conn,
                run_id,
                source,
                started_at,
                parser_hash,
                chunker_hash,
                "failed",
                error=error,
            )

    @staticmethod
    def _store_generation(conn: Connection, output: ProcessingOutput) -> None:
        generation = output.generation
        key = sa.and_(
            generations.c.raw_version_id == generation["raw_version_id"],
            generations.c.parser_config_sha256 == generation["parser_config_sha256"],
        )
        if conn.scalar(sa.select(generations.c.generation_id).where(key)) is None:
            conn.execute(generations.insert(), generation)
            conn.execute(blocks.insert(), output.blocks)
        else:
            _check_rows(conn, generations, [generation], key)
            _check_rows(
                conn,
                blocks,
                output.blocks,
                blocks.c.generation_id == generation["generation_id"],
                blocks.c.block_ordinal,
            )

    @staticmethod
    def _store_set(conn: Connection, output: ProcessingOutput) -> Outcome:
        passage_set = output.passage_set
        set_id = passage_set["passage_set_id"]
        key = sa.and_(
            sets.c.generation_id == passage_set["generation_id"],
            sets.c.chunker_config_sha256 == passage_set["chunker_config_sha256"],
        )
        if conn.scalar(sa.select(sets.c.passage_set_id).where(key)) is None:
            conn.execute(sets.insert(), passage_set)
            conn.execute(passages.insert(), output.passages)
            conn.execute(spans.insert(), output.spans)
            return "created"
        _check_rows(conn, sets, [passage_set], key)
        if (
            conn.scalar(
                sa.select(attempts.c.attempt_id).where(
                    attempts.c.passage_set_id == set_id, attempts.c.outcome == "created"
                )
            )
            is None
        ):
            raise ProcessingError("output_conflict")
        _check_rows(
            conn,
            passages,
            output.passages,
            passages.c.passage_set_id == set_id,
            passages.c.passage_ordinal,
        )
        expected_spans = sorted(
            output.spans, key=lambda row: (row["passage_id"], row["span_ordinal"])
        )
        _check_rows(
            conn,
            spans,
            expected_spans,
            spans.c.passage_id.in_(
                sa.select(passages.c.passage_id).where(passages.c.passage_set_id == set_id)
            ),
            spans.c.passage_id.collate("C"),
            spans.c.span_ordinal,
        )
        return "reused"

    def publish(
        self,
        run_id: UUID,
        source: ProcessingInput,
        started_at: datetime,
        output: ProcessingOutput,
    ) -> ProcessingResult:
        parser_hash = output.generation["parser_config_sha256"]
        chunker_hash = output.passage_set["chunker_config_sha256"]
        with self.engine.begin() as conn:
            if not self._lock_current(conn, source):
                return self._write_audit(
                    conn,
                    run_id,
                    source,
                    started_at,
                    parser_hash,
                    chunker_hash,
                    "stale",
                    error="stale_raw_version",
                )
            self._store_generation(conn, output)
            outcome = self._store_set(conn, output)
            return self._write_audit(
                conn,
                run_id,
                source,
                started_at,
                parser_hash,
                chunker_hash,
                outcome,
                output,
            )
