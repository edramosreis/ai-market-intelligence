import pytest

from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.chunking import ChunkerConfig, chunk_document
from market_intelligence.fomc.passage_models import (
    Block,
    BlockKind,
    FomcParseError,
    ParsedDocument,
    ParserConfig,
    Reference,
    ReferenceKind,
    build_document,
)
from market_intelligence.fomc.validation import validate_calendar, validate_document
from tests.fomc_fakes import calendar_body, fetched


def document(*blocks: Block) -> ParsedDocument:
    validated = validate_document(fetched(PILOT_DOCUMENTS[0]), validate_calendar(calendar_body()))
    return build_document(validated, blocks, ParserConfig())


def test_hand_checked_packing_keeps_section_boundaries_and_exact_spans() -> None:
    doc = document(
        Block(0, BlockKind.PARAGRAPH, "Alpha."),
        Block(1, BlockKind.PARAGRAPH, "Beta."),
        Block(2, BlockKind.HEADING, "Outlook", section_path=(2,)),
        Block(3, BlockKind.PARAGRAPH, "Gamma.", section_path=(2,)),
    )
    passages = chunk_document(doc, ChunkerConfig(target_chars=25, max_chars=30))
    assert [p.text for p in passages] == ["Alpha.\n\nBeta.", "Gamma."]
    assert [(s.block, s.start, s.end) for s in passages[0].spans] == [(0, 0, 6), (1, 0, 5)]
    assert passages[1].section_path == (2,)
    assert passages == chunk_document(doc, ChunkerConfig(target_chars=25, max_chars=30))
    assert (
        passages[0].passage_id
        != chunk_document(
            doc, ChunkerConfig(version="fomc-chunker-v2", target_chars=25, max_chars=30)
        )[0].passage_id
    )


@pytest.mark.parametrize("text", ["A" * 2401, "Î±Î²Î³ " * 900, "Value 1.25 remains. " * 250])
def test_fragments_reconstruct_every_unicode_character_without_overlap(text: str) -> None:
    text = text.strip()
    doc = document(Block(0, BlockKind.PARAGRAPH, text))
    passages = chunk_document(doc)
    assert "".join(p.text for p in passages) == text
    assert all(1 <= len(p.text) <= 2400 and p.fragmented for p in passages)
    end = 0
    for p in passages:
        span = p.spans[0]
        assert span.start == end
        assert p.text == doc.blocks[0].text[span.start : span.end]
        end = span.end
    assert end == len(text)


def test_sentence_preference_does_not_treat_initialisms_as_a_sentence() -> None:
    text = "U.S. policy remains stable. Next review follows after 1.25 percent."
    passages = chunk_document(
        document(Block(0, BlockKind.PARAGRAPH, text)), ChunkerConfig(target_chars=28, max_chars=34)
    )
    assert passages[0].text == "U.S. policy remains stable. "
    assert "".join(p.text for p in passages) == text


def test_soft_target_keeps_a_complete_block_up_to_the_hard_cap() -> None:
    doc = document(Block(0, BlockKind.PARAGRAPH, "A" * 2400))
    passages = chunk_document(doc)
    assert len(passages) == 1 and not passages[0].fragmented


def test_auxiliary_and_footnote_text_remain_separate_from_policy_passages() -> None:
    doc = document(
        Block(0, BlockKind.PARAGRAPH, "Policy."),
        Block(1, BlockKind.AUXILIARY, "Contact."),
        Block(2, BlockKind.FOOTNOTE, "Note."),
    )
    assert [p.text for p in chunk_document(doc)] == ["Policy.", "Contact.", "Note."]


def test_reference_labels_are_not_split_and_cannot_bypass_work_limits() -> None:
    text = "A " + ("x" * 30) + " suffix"
    block = Block(
        0,
        BlockKind.PARAGRAPH,
        text,
        references=(Reference("fn1", "x" * 30, 2, 32, ReferenceKind.FOOTNOTE),),
    )
    with pytest.raises(FomcParseError, match="unbreakable_reference"):
        chunk_document(document(block), ChunkerConfig(target_chars=10, max_chars=20))


def test_passage_limit_does_not_return_partial_results() -> None:
    doc = document(Block(0, BlockKind.PARAGRAPH, "A" * 2401))
    with pytest.raises(FomcParseError, match="fomc_passage_limit"):
        chunk_document(doc, ChunkerConfig(max_passages=1))


@pytest.mark.parametrize(
    "config",
    [dict(target_chars=0), dict(max_chars=2401), dict(target_chars=True), dict(max_passages=1025)],
)
def test_invalid_chunk_configuration_rejected(config: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ChunkerConfig(
            target_chars=config.get("target_chars", 1800),
            max_chars=config.get("max_chars", 2400),
            max_passages=config.get("max_passages", 1024),
        )
