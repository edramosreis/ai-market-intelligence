"""Process previously acquired FOMC originals without a connection during file work."""

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.db.fomc_processing_store import FomcProcessingStore
from market_intelligence.fomc.artifacts import ArtifactError, ArtifactStore
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.chunking import (
    DEFAULT_CHUNKER_CONFIG,
    ChunkerConfig,
    chunk_document,
)
from market_intelligence.fomc.client import FetchedDocument
from market_intelligence.fomc.parsing import DEFAULT_PARSER_CONFIG, parse_document
from market_intelligence.fomc.passage_models import FomcParseError, ParserConfig
from market_intelligence.fomc.processing_models import (
    ProcessingError,
    ProcessingInput,
    ProcessingOutput,
    ProcessingResult,
    prepare_output,
)
from market_intelligence.fomc.validation import FomcValidationError, validate_calendar
from market_intelligence.time_utils import utc_now


@dataclass(frozen=True)
class ProcessingRun:
    run_id: UUID
    documents: tuple[ProcessingResult, ...]


def process_original(
    source: ProcessingInput,
    artifacts: ArtifactStore,
    parser: ParserConfig,
    chunker: ChunkerConfig,
) -> ProcessingOutput:
    body = artifacts.read_verified(source.raw_sha256, source.byte_count)
    calendar_body = artifacts.read_verified(source.calendar_sha256, source.calendar_bytes)
    raw = FetchedDocument(
        source.spec,
        body,
        source.receipt_id,
        source.fetch_started_at,
        source.received_at,
        source.content_type,
        source.content_encoding,
        source.wire_bytes,
        source.etag,
        source.last_modified,
    )
    if raw.raw_version_id != source.raw_version_id:
        raise ProcessingError("identity_mismatch")
    try:
        calendar = validate_calendar(calendar_body, source.calendar_content_type)
        parsed = parse_document(raw, calendar, parser)
    except FomcValidationError, FomcParseError:
        raise ProcessingError("parse_failed") from None
    try:
        passages = chunk_document(parsed, chunker)
    except FomcParseError:
        raise ProcessingError("chunk_failed") from None
    return prepare_output(source, parsed, passages, chunker)


def process_pilot(
    store: FomcProcessingStore,
    artifacts: ArtifactStore,
    parser: ParserConfig = DEFAULT_PARSER_CONFIG,
    chunker: ChunkerConfig = DEFAULT_CHUNKER_CONFIG,
) -> ProcessingRun:
    sources = store.inputs()
    run_id = uuid4()
    results: list[ProcessingResult] = []
    for spec in PILOT_DOCUMENTS:
        source = sources.get(spec.document_id)
        if source is None:
            results.append(ProcessingResult(spec.document_id, "not_collected"))
            continue
        started_at = utc_now()
        try:
            output = process_original(source, artifacts, parser, chunker)
            results.append(store.publish(run_id, source, started_at, output))
            continue
        except ArtifactError:
            error = "artifact_error"
        except ProcessingError as exc:
            error = exc.code
        except SQLAlchemyError:
            error = "database_error"
        except ValueError, OSError, RuntimeError:
            error = "internal_error"
        try:
            results.append(store.failure(run_id, source, started_at, parser, chunker, error))
        except SQLAlchemyError:
            results.append(
                ProcessingResult(
                    spec.document_id,
                    "failed",
                    source.raw_version_id,
                    error_code=error,
                )
            )
    return ProcessingRun(run_id, tuple(results))
