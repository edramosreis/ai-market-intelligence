"""Deterministic lossless passage packing over canonical block coordinates."""

import hashlib
import re
from dataclasses import asdict, dataclass

from market_intelligence.fomc.passage_models import (
    MAX_PASSAGES,
    Block,
    BlockKind,
    BlockSpan,
    FomcParseError,
    ParsedDocument,
    Passage,
    fingerprint,
)

SEPARATOR = "\n\n"
ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "prof.", "no.", "vs.", "etc."}


@dataclass(frozen=True)
class ChunkerConfig:
    version: str = "fomc-chunker-v1"
    target_chars: int = 1800
    max_chars: int = 2400
    max_passages: int = MAX_PASSAGES

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", self.version)
            or any(
                type(v) is not int for v in (self.target_chars, self.max_chars, self.max_passages)
            )
            or not 1 <= self.target_chars <= self.max_chars <= 2400
            or not 1 <= self.max_passages <= MAX_PASSAGES
        ):
            raise ValueError("Invalid chunker configuration")

    @property
    def config_hash(self) -> str:
        return fingerprint(
            "fomc-chunker-config-v1",
            asdict(self),
            {
                "separator": SEPARATOR,
                "sentence": "punctuation-space-v1",
                "abbreviations": sorted(ABBREVIATIONS),
                "minimum_split": "half-target",
                "fallback": "whitespace-then-code-point",
                "overlap": 0,
            },
        )


DEFAULT_CHUNKER_CONFIG = ChunkerConfig()


def _split_end(block: Block, start: int, config: ChunkerConfig) -> int:
    hard = min(len(block.text), start + config.max_chars)
    if hard == len(block.text):
        return hard
    target = start + config.target_chars
    minimum = start + max(1, config.target_chars // 2)

    def safe(end: int) -> bool:
        return not any(r.start < end < r.end for r in block.references)

    sentences = []
    for match in re.finditer(r"""[.!?]["'”’)]*\s+""", block.text[start:hard]):
        end = start + match.end()
        punctuation = start + match.start() + 1
        token = re.search(r"[A-Za-z.]+$", block.text[start:punctuation])
        if token and (
            token[0].lower() in ABBREVIATIONS or re.fullmatch(r"(?:[A-Za-z]\.)+", token[0])
        ):
            continue
        if end >= minimum and safe(end):
            sentences.append(end)
    spaces = [
        start + m.end()
        for m in re.finditer(r"\s+", block.text[start:hard])
        if start + m.end() >= minimum and safe(start + m.end())
    ]
    for candidates in (sentences, spaces):
        before = [end for end in candidates if end <= target]
        if before:
            return before[-1]
        if candidates:
            return candidates[-1]
    if safe(hard):
        return hard
    # Never split an inspectable link label across fragments.
    boundary = min(r.start for r in block.references if r.start < hard < r.end)
    if boundary <= start:
        raise FomcParseError("fomc_unbreakable_reference")
    return boundary


def chunk_document(
    document: ParsedDocument, config: ChunkerConfig = DEFAULT_CHUNKER_CONFIG
) -> tuple[Passage, ...]:
    output: list[Passage] = []
    pending: list[BlockSpan] = []
    size = 0
    group: object = None

    def publish(spans: tuple[BlockSpan, ...]) -> None:
        if len(output) >= config.max_passages:
            raise FomcParseError("fomc_passage_limit")
        text = SEPARATOR.join(document.blocks[s.block].text[s.start : s.end] for s in spans)
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        output.append(
            Passage(
                fingerprint(
                    "fomc-passage-v1",
                    document.generation_id,
                    config.version,
                    config.config_hash,
                    [asdict(s) for s in spans],
                    text_hash,
                ),
                document.generation_id,
                text,
                text_hash,
                document.blocks[spans[0].block].section_path,
                spans,
                any(s.start > 0 or s.end < len(document.blocks[s.block].text) for s in spans),
                config.config_hash,
            )
        )

    def flush() -> None:
        nonlocal size
        if pending:
            publish(tuple(pending))
            pending.clear()
        size = 0

    for block in document.blocks:
        if block.kind == BlockKind.HEADING:
            flush()
            continue
        key = (block.section_path, block.quote_path, block.list_path, block.kind)
        if key != group:
            flush()
            group = key
        if len(block.text) > config.max_chars:
            flush()
            start = 0
            while start < len(block.text):
                end = _split_end(block, start, config)
                publish((BlockSpan(block.ordinal, start, end),))
                start = end
            continue
        if pending and size + len(SEPARATOR) + len(block.text) > config.target_chars:
            flush()
        pending.append(BlockSpan(block.ordinal, 0, len(block.text)))
        size += len(block.text) + (len(SEPARATOR) if len(pending) > 1 else 0)
    flush()
    return tuple(output)
