"""Build and check immutable database records from the offline processing output."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from market_intelligence.fomc.chunking import SEPARATOR, ChunkerConfig
from market_intelligence.fomc.models import DocumentSpec
from market_intelligence.fomc.passage_models import (
    BlockKind,
    ParsedDocument,
    Passage,
    ReferenceKind,
    fingerprint,
)

Row = dict[str, Any]
Outcome = Literal["created", "reused", "stale", "failed", "not_collected"]


class ProcessingError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProcessingInput:
    spec: DocumentSpec
    raw_version_id: str
    raw_sha256: str
    byte_count: int
    receipt_id: UUID
    fetch_started_at: datetime
    received_at: datetime
    content_type: str
    content_encoding: str
    wire_bytes: int
    etag: str | None
    last_modified: str | None
    calendar_sha256: str
    calendar_bytes: int
    calendar_content_type: str


@dataclass(frozen=True)
class ProcessingResult:
    document_id: str
    outcome: Outcome
    raw_version_id: str | None = None
    passage_set_id: str | None = None
    block_count: int = 0
    passage_count: int = 0
    error_code: str | None = None
    audit_recorded: bool = False


@dataclass(frozen=True)
class ProcessingOutput:
    generation: Row
    blocks: list[Row]
    passage_set: Row
    passages: list[Row]
    spans: list[Row]


def _json_value(value: object) -> Any:
    # Normalize tuples (and nested dataclasses already converted by asdict) to JSON lists.
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _check_coverage(parsed: ParsedDocument, passages: tuple[Passage, ...]) -> None:
    if not parsed.blocks or any(b.ordinal != i for i, b in enumerate(parsed.blocks)):
        raise ProcessingError("output_conflict")
    anchors = [anchor.name for block in parsed.blocks for anchor in block.anchors]
    anchor_names = set(anchors)
    if len(anchor_names) != len(anchors):
        raise ProcessingError("output_conflict")
    for block in parsed.blocks:
        if any(
            not 0 <= i <= block.ordinal or parsed.blocks[i].kind != BlockKind.HEADING
            for i in block.section_path
        ) or any(
            reference.target not in anchor_names and reference.kind != ReferenceKind.LOCAL
            for reference in block.references
        ):
            raise ProcessingError("output_conflict")

    expected = [
        (block.ordinal, len(block.text))
        for block in parsed.blocks
        if block.kind != BlockKind.HEADING
    ]
    block_index = offset = 0
    for passage in passages:
        if not passage.spans:
            raise ProcessingError("output_conflict")
        pieces = []
        fragmented = False
        previous_context = None
        for span in passage.spans:
            if block_index == len(expected):
                raise ProcessingError("output_conflict")
            ordinal, length = expected[block_index]
            if span.block != ordinal or span.start != offset or not span.start < span.end <= length:
                raise ProcessingError("output_conflict")
            block = parsed.blocks[span.block]
            context = (block.kind, block.section_path, block.quote_path, block.list_path)
            if (
                block.section_path != passage.section_path
                or (previous_context is not None and previous_context != context)
                or any(
                    r.start < value < r.end
                    for r in block.references
                    for value in (span.start, span.end)
                )
            ):
                raise ProcessingError("output_conflict")
            previous_context = context
            pieces.append(block.text[span.start : span.end])
            fragmented |= span.start > 0 or span.end < length
            offset = span.end
            if offset == length:
                block_index += 1
                offset = 0
        if SEPARATOR.join(pieces) != passage.text or fragmented != passage.fragmented:
            raise ProcessingError("output_conflict")
    if block_index != len(expected) or offset:
        raise ProcessingError("output_conflict")


def prepare_output(
    source: ProcessingInput,
    parsed: ParsedDocument,
    passages: tuple[Passage, ...],
    chunker: ChunkerConfig,
) -> ProcessingOutput:
    if (parsed.document, parsed.raw_version_id, parsed.raw_sha256, parsed.calendar_sha256) != (
        source.spec,
        source.raw_version_id,
        source.raw_sha256,
        source.calendar_sha256,
    ):
        raise ProcessingError("identity_mismatch")
    if not 1 <= len(passages) <= chunker.max_passages:
        raise ProcessingError("chunk_failed")
    _check_coverage(parsed, passages)
    body_bytes = SEPARATOR.join(block.text for block in parsed.blocks).encode()
    generation_id = parsed.generation_id
    parser_hash = parsed.config.config_hash
    chunker_hash = chunker.config_hash
    structured = [asdict(block) for block in parsed.blocks]
    content_hash = fingerprint("fomc-blocks-v1", structured, parsed.issues)
    if (
        len(parsed.blocks) > parsed.config.max_blocks
        or len(body_bytes) > parsed.config.max_text_bytes
        or content_hash != parsed.content_sha256
        or hashlib.sha256(body_bytes).hexdigest() != parsed.text_sha256
        or generation_id
        != fingerprint(
            "fomc-processing-v1",
            source.spec.document_id,
            source.raw_version_id,
            parsed.config.version,
            parser_hash,
            content_hash,
        )
    ):
        raise ProcessingError("output_conflict")
    set_id = fingerprint("fomc-passage-set-v1", generation_id, chunker_hash)
    generation = dict(
        generation_id=generation_id,
        document_id=source.spec.document_id,
        raw_version_id=source.raw_version_id,
        raw_sha256=source.raw_sha256,
        raw_byte_count=source.byte_count,
        parser_version=parsed.config.version,
        parser_config_sha256=parser_hash,
        parser_config=_json_value(parsed.config.hash_arguments),
        validator_version=parsed.validator_version,
        content_sha256=content_hash,
        text_sha256=parsed.text_sha256,
        block_count=len(parsed.blocks),
        canonical_bytes=len(body_bytes),
        issues=list(parsed.issues),
    )
    blocks: list[Row] = []
    for block in structured:
        block = _json_value(block)
        block["generation_id"] = generation_id
        block["block_ordinal"] = block.pop("ordinal")
        block["body"] = block.pop("text")
        block["references_json"] = block.pop("references")
        blocks.append(block)
    rows: list[Row] = []
    spans: list[Row] = []
    for index, passage in enumerate(passages):
        text_hash = hashlib.sha256(passage.text.encode()).hexdigest()
        passage_id = fingerprint(
            "fomc-passage-v1",
            generation_id,
            chunker.version,
            chunker_hash,
            [asdict(s) for s in passage.spans],
            text_hash,
        )
        if (
            passage.passage_id != passage_id
            or passage.generation_id != generation_id
            or passage.config_hash != chunker_hash
            or passage.text_sha256 != text_hash
            or not 1 <= len(passage.text) <= chunker.max_chars
        ):
            raise ProcessingError("output_conflict")
        rows.append(
            dict(
                passage_id=passage_id,
                passage_set_id=set_id,
                generation_id=generation_id,
                passage_ordinal=index,
                body=passage.text,
                text_sha256=text_hash,
                section_path=list(passage.section_path),
                fragmented=passage.fragmented,
            )
        )
        spans.extend(
            dict(
                passage_id=passage_id,
                generation_id=generation_id,
                span_ordinal=i,
                block_ordinal=s.block,
                start_offset=s.start,
                end_offset=s.end,
            )
            for i, s in enumerate(passage.spans)
        )
    passage_set = dict(
        passage_set_id=set_id,
        generation_id=generation_id,
        chunker_version=chunker.version,
        chunker_config_sha256=chunker_hash,
        chunker_config=_json_value(chunker.hash_arguments),
        manifest_sha256=fingerprint("fomc-passage-manifest-v1", [p.passage_id for p in passages]),
        passage_count=len(passages),
    )
    return ProcessingOutput(generation, blocks, passage_set, rows, spans)
