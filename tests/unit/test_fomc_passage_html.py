import pytest

from market_intelligence.fomc.passage_html import article_tree, inline_text
from market_intelligence.fomc.passage_models import Anchor, FomcParseError, ReferenceKind
from market_intelligence.fomc.validation import _Node


def tree(body: str) -> _Node:
    return article_tree(f'<div id="article">{body}</div>'.encode())


def test_inline_text_keeps_adjacent_numbers_and_exact_reference_offsets() -> None:
    root = tree(
        ' \nRate <span>1</span>.25% â€” Î±<sup><a href="#fn1">1</a></sup><a name="f1"></a> '
    )
    result = inline_text(root.children)
    assert result.text == "Rate 1.25% â€” Î±1"
    assert result.anchors == (Anchor("f1", len(result.text)),)
    assert result.references[0].label == "1"
    assert result.text[result.references[0].start : result.references[0].end] == "1"
    assert result.references[0].kind == ReferenceKind.FOOTNOTE


def test_footnote_backlink_retains_metadata_without_navigation_prose() -> None:
    root = tree('<a name="fn1"></a>1. Note text. <a href="#f1">Return to text</a>')
    result = inline_text(root.children)
    assert result.text == "1. Note text."
    assert result.anchors == (Anchor("fn1", 0),)
    assert result.references[0].kind == ReferenceKind.BACKLINK
    assert result.references[0].label == ""


def test_whitespace_exclusions_and_unicode_are_explicit() -> None:
    result = inline_text(
        tree("Î±&nbsp; âˆ’0.25 <script>execute()</script><svg>bad</svg><br>Î²").children
    )
    assert result.text == "Î± âˆ’0.25 Î²"


@pytest.mark.parametrize(
    "body", ["<table><tr><td>1</td></tr></table>", '<img src="bad">', "<strong>unterminated"]
)
def test_unsupported_inline_content_is_rejected(body: str) -> None:
    with pytest.raises(FomcParseError):
        inline_text(tree(body).children)


def test_duplicate_named_anchor_attribute_rejected() -> None:
    with pytest.raises(FomcParseError, match="ambiguous"):
        tree('<a name="a" name="b">anchor</a>')


def test_ordinary_fragment_is_not_guessed_to_be_a_footnote() -> None:
    result = inline_text(tree('<a href="#financial">Section</a>').children)
    assert result.references[0].kind == ReferenceKind.LOCAL
