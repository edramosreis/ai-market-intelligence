"""Immutable offline processing output; no persistence, network or model access."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from enum import StrEnum

from market_intelligence.fomc.models import DocumentSpec
from market_intelligence.fomc.validation import ValidatedDocument

MAX_BLOCKS = 2000
MAX_TEXT_BYTES = 1024 * 1024
MAX_PASSAGES = 1024


class FomcParseError(ValueError):
    def __init__(self, code: str = "unsupported_fomc_layout") -> None:
        super().__init__(code)


def fingerprint(namespace: str, *values: object) -> str:
    encoded = json.dumps(
        [namespace, *values],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ParserConfig:
    version: str = "fomc-parser-v1"
    max_blocks: int = MAX_BLOCKS
    max_text_bytes: int = MAX_TEXT_BYTES

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", self.version):
            raise ValueError("Invalid parser version")
        if (
            type(self.max_blocks) is not int
            or not 1 <= self.max_blocks <= MAX_BLOCKS
            or type(self.max_text_bytes) is not int
            or not 1 <= self.max_text_bytes <= MAX_TEXT_BYTES
        ):
            raise ValueError("Invalid parser bounds")

    @property
    def config_hash(self) -> str:
        return fingerprint(
            "fomc-parser-config-v1",
            asdict(self),
            {
                "input_bytes": 2097152,
                "nodes": 25000,
                "depth": 128,
                "whitespace": "inline-space-block-separator-v1",
            },
        )


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    QUOTE = "quote_paragraph"
    LIST_ITEM = "list_item"
    FOOTNOTE = "footnote"
    AUXILIARY = "auxiliary"


class ReferenceKind(StrEnum):
    LOCAL = "local"
    FOOTNOTE = "footnote"
    BACKLINK = "backlink"


@dataclass(frozen=True)
class Anchor:
    name: str
    offset: int


@dataclass(frozen=True)
class Reference:
    target: str
    label: str
    start: int
    end: int
    kind: ReferenceKind


@dataclass(frozen=True)
class ListPosition:
    ordered: bool
    position: int


@dataclass(frozen=True)
class Block:
    ordinal: int
    kind: BlockKind
    text: str
    section_path: tuple[int, ...] = ()
    quote_path: tuple[int, ...] = ()
    list_path: tuple[ListPosition, ...] = ()
    anchors: tuple[Anchor, ...] = ()
    references: tuple[Reference, ...] = ()

    def __post_init__(self) -> None:
        if self.ordinal < 0 or not self.text or " ".join(self.text.split()) != self.text:
            raise ValueError("Invalid canonical block")
        if any(a.offset < 0 or a.offset > len(self.text) or not a.name for a in self.anchors):
            raise ValueError("Invalid anchor position")
        if any(
            not r.target
            or not 0 <= r.start <= r.end <= len(self.text)
            or self.text[r.start : r.end] != r.label
            for r in self.references
        ):
            raise ValueError("Invalid reference position")


@dataclass(frozen=True)
class ParsedDocument:
    document: DocumentSpec
    raw_version_id: str
    raw_sha256: str
    title: str
    meeting_label: str
    publication_label: str
    release_time_text: str | None
    calendar_sha256: str
    validator_version: str
    config: ParserConfig
    blocks: tuple[Block, ...]
    content_sha256: str
    text_sha256: str
    generation_id: str
    issues: tuple[str, ...] = ()


def build_document(
    validated: ValidatedDocument,
    blocks: tuple[Block, ...],
    config: ParserConfig,
    issues: tuple[str, ...] = (),
) -> ParsedDocument:
    if not blocks or len(blocks) > config.max_blocks:
        raise FomcParseError("fomc_block_limit")
    if any(block.ordinal != ordinal for ordinal, block in enumerate(blocks)):
        raise ValueError("Nonconsecutive blocks")
    text = "\n\n".join(block.text for block in blocks)
    if len(text.encode("utf-8")) > config.max_text_bytes:
        raise FomcParseError("fomc_text_limit")
    for block in blocks:
        if any(
            index > block.ordinal or blocks[index].kind != BlockKind.HEADING
            for index in block.section_path
        ):
            raise ValueError("Invalid section path")
    content_hash = fingerprint("fomc-blocks-v1", [asdict(b) for b in blocks], issues)
    raw = validated.fetched
    return ParsedDocument(
        raw.document,
        raw.raw_version_id,
        raw.raw_sha256,
        validated.title,
        validated.meeting_label,
        validated.publication_label,
        validated.release_time_text,
        validated.calendar_sha256,
        validated.validator_version,
        config,
        blocks,
        content_hash,
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
        fingerprint(
            "fomc-processing-v1",
            raw.document.document_id,
            raw.raw_version_id,
            config.version,
            config.config_hash,
            content_hash,
        ),
        issues,
    )


@dataclass(frozen=True)
class BlockSpan:
    block: int
    start: int
    end: int


@dataclass(frozen=True)
class Passage:
    passage_id: str
    generation_id: str
    text: str
    text_sha256: str
    section_path: tuple[int, ...]
    spans: tuple[BlockSpan, ...]
    fragmented: bool
    config_hash: str
