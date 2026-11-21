from dataclasses import replace

import pytest

from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.passage_models import (
    Anchor,
    Block,
    BlockKind,
    FomcParseError,
    ParserConfig,
    Reference,
    ReferenceKind,
    build_document,
)
from market_intelligence.fomc.validation import validate_calendar, validate_document
from tests.fomc_fakes import calendar_body, fetched


def test_generation_identity_preserves_raw_lineage_and_configuration() -> None:
    raw = fetched(PILOT_DOCUMENTS[0])
    validated = validate_document(raw, validate_calendar(calendar_body()))
    blocks = (Block(0, BlockKind.PARAGRAPH, "Rate −0.25%; index 300.1."),)
    first = build_document(validated, blocks, ParserConfig())
    assert first == build_document(validated, blocks, ParserConfig())
    cosmetic = replace(validated, fetched=replace(raw, body=raw.body + b"\n"))
    second = build_document(cosmetic, blocks, ParserConfig())
    assert second.content_sha256 == first.content_sha256
    assert second.text_sha256 == first.text_sha256
    assert second.generation_id != first.generation_id
    revised = build_document(validated, blocks, ParserConfig(version="fomc-parser-v2"))
    assert revised.generation_id != first.generation_id
    noted = build_document(
        validated, (replace(blocks[0], anchors=(Anchor("note", 0),)),), ParserConfig()
    )
    assert noted.content_sha256 != first.content_sha256
    assert noted.text_sha256 == first.text_sha256


def test_canonical_offsets_are_unicode_code_points() -> None:
    block = Block(
        0,
        BlockKind.PARAGRAPH,
        "α 1.25¹",
        anchors=(Anchor("ref", 6),),
        references=(Reference("fn1", "¹", 6, 7, ReferenceKind.FOOTNOTE),),
    )
    assert block.text[block.references[0].start : block.references[0].end] == "¹"
    with pytest.raises(ValueError, match="reference"):
        replace(block, references=(replace(block.references[0], start=5),))


@pytest.mark.parametrize(
    "blocks,limit,code",
    [
        (
            (Block(0, BlockKind.PARAGRAPH, "a"), Block(1, BlockKind.PARAGRAPH, "b")),
            ParserConfig(max_blocks=1),
            "fomc_block_limit",
        ),
        ((Block(0, BlockKind.PARAGRAPH, "α"),), ParserConfig(max_text_bytes=1), "fomc_text_limit"),
    ],
)
def test_processing_bounds_fail_without_partial_output(
    blocks: tuple[Block, ...], limit: ParserConfig, code: str
) -> None:
    validated = validate_document(fetched(PILOT_DOCUMENTS[0]), validate_calendar(calendar_body()))
    with pytest.raises(FomcParseError, match=code):
        build_document(validated, blocks, limit)


def test_section_paths_cannot_point_at_prose() -> None:
    validated = validate_document(fetched(PILOT_DOCUMENTS[0]), validate_calendar(calendar_body()))
    with pytest.raises(ValueError, match="section"):
        build_document(
            validated, (Block(0, BlockKind.PARAGRAPH, "body", section_path=(0,)),), ParserConfig()
        )
