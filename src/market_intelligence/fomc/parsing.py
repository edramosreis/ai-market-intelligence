"""Pure bounded Board layout adapter over already acquired originals."""

import re
from urllib.parse import urljoin

from market_intelligence.fomc.client import FetchedDocument
from market_intelligence.fomc.models import DocumentKind
from market_intelligence.fomc.passage_html import (
    InlineText,
    article_tree,
    hidden,
    inline_text,
    names,
)
from market_intelligence.fomc.passage_models import (
    Anchor,
    Block,
    BlockKind,
    FomcParseError,
    ListPosition,
    ParsedDocument,
    ParserConfig,
    ReferenceKind,
    build_document,
)
from market_intelligence.fomc.validation import (
    CalendarEvidence,
    FomcValidationError,
    ValidatedDocument,
    _dash,
    _Node,
    validate_document,
)

MINUTES_SECTIONS = {
    "Developments in Financial Markets and Open Market Operations",
    "Staff Review of the Economic Situation",
    "Staff Review of the Financial Situation",
    "Staff Economic Outlook",
    "Participants' Views on Current Conditions and the Economic Outlook",
    "Committee Policy Actions",
    "Notation Vote",
    "Attendance",
}
DEFAULT_PARSER_CONFIG = ParserConfig()
CONTAINERS = {"div", "section", "article", "main"}
BLOCK_TAGS = {"p", "h3", "h4", "h5", "h6", "blockquote", "ul", "ol", "li", "hr", *CONTAINERS}


class _Extractor:
    def __init__(self, validated: ValidatedDocument, config: ParserConfig) -> None:
        self.validated, self.config = validated, config
        self.blocks: list[Block] = []
        self.sections: list[tuple[int, int]] = []
        self.pending: list[Anchor] = []
        self.quote_count = 0
        self.text_bytes = 0
        self.dateline_pending = validated.fetched.document.kind == DocumentKind.MINUTES
        self.notes_region = False

    def emit(
        self,
        value: InlineText,
        kind: BlockKind,
        quote: tuple[int, ...],
        listing: tuple[ListPosition, ...],
        level: int = 0,
    ) -> None:
        if not value.text:
            self.pending.extend(value.anchors)
            if value.references:
                raise FomcParseError("empty_fomc_reference_block")
            return
        ordinal = len(self.blocks)
        if ordinal >= self.config.max_blocks:
            raise FomcParseError("fomc_block_limit")
        self.text_bytes += len(value.text.encode("utf-8")) + (2 if ordinal else 0)
        if self.text_bytes > self.config.max_text_bytes:
            raise FomcParseError("fomc_text_limit")
        if kind == BlockKind.HEADING:
            while self.sections and self.sections[-1][0] >= level:
                self.sections.pop()
            self.sections.append((level, ordinal))
        anchors = tuple(self.pending) + value.anchors
        self.pending.clear()
        self.blocks.append(
            Block(
                ordinal,
                kind,
                value.text,
                () if kind == BlockKind.FOOTNOTE else tuple(i for _, i in self.sections),
                quote,
                listing,
                anchors,
                value.references,
            )
        )

    def paragraph(
        self, node: _Node, quote: tuple[int, ...], listing: tuple[ListPosition, ...]
    ) -> None:
        value = inline_text(node.children)
        if self.dateline_pending:
            label = self.validated.meeting_label
            if not re.match(re.escape(_dash(label)) + r"(?:\s|$)", _dash(value.text)):
                raise FomcParseError("invalid_fomc_dateline")
            if self.pending or any(a.offset < len(label) for a in value.anchors):
                raise FomcParseError("anchored_fomc_metadata")
            value = value.portion(len(label), len(value.text))
            self.dateline_pending = False
        visible = [c for c in node.children if not isinstance(c, str) or c.strip()]
        if (
            visible
            and isinstance(visible[0], _Node)
            and visible[0].tag == "strong"
            and not quote
            and not listing
        ):
            leading = inline_text(visible[0].children).text
            boundary = len(visible) == 1 or (
                isinstance(visible[1], _Node) and visible[1].tag == "br"
            )
            if leading in MINUTES_SECTIONS and boundary:
                self.emit(value.portion(0, len(leading)), BlockKind.HEADING, quote, listing, 4)
                value = value.portion(len(leading), len(value.text))
        kind = BlockKind.LIST_ITEM if listing else BlockKind.QUOTE if quote else BlockKind.PARAGRAPH
        if any(re.fullmatch(r"fn[0-9]+", a.name) for a in value.anchors):
            kind = BlockKind.FOOTNOTE
        elif self.notes_region and value.text:
            raise FomcParseError("unmarked_fomc_footnote")
        elif self.auxiliary(node, value.text):
            kind = BlockKind.AUXILIARY
        self.emit(value, kind, quote, listing)

    def auxiliary(self, node: _Node, text: str) -> bool:
        if re.fullmatch(r"_+", text):
            return True
        nodes = node.nodes()
        links = [n for n in nodes if n.tag == "a"]
        email_link = any(
            (n.attrs.get("href") or "").startswith(("mailto:", "/cdn-cgi/l/email-protection"))
            for n in links
        )
        protected_email = any("__cf_email__" in (n.attrs.get("class") or "").split() for n in nodes)
        if text.startswith("For media inquiries, please email ") and (
            email_link or protected_email
        ):
            return True
        spec = self.validated.fetched.document
        expected = f"/newsevents/pressreleases/monetary{spec.published_on:%Y%m%d}a1.htm"
        return text.startswith("Implementation Note issued ") and any(
            urljoin(spec.url, n.attrs.get("href") or "")
            == "https://www.federalreserve.gov" + expected
            for n in links
        )

    def container(
        self, node: _Node, quote: tuple[int, ...] = (), listing: tuple[ListPosition, ...] = ()
    ) -> None:
        buffer: list[_Node | str] = []

        def flush() -> None:
            if buffer:
                self.emit(
                    inline_text(buffer),
                    BlockKind.LIST_ITEM
                    if listing
                    else BlockKind.QUOTE
                    if quote
                    else BlockKind.PARAGRAPH,
                    quote,
                    listing,
                )
                buffer.clear()

        for child in node.children:
            if isinstance(child, str) or child.tag not in BLOCK_TAGS:
                buffer.append(child)
            else:
                flush()
                self.node(child, quote, listing)
        flush()

    def node(self, node: _Node, quote: tuple[int, ...], listing: tuple[ListPosition, ...]) -> None:
        if hidden(node):
            return
        if not node.complete:
            raise FomcParseError("incomplete_fomc_block")
        if node.tag == "h3" and node.text() == self.validated.title:
            return
        self.pending.extend(Anchor(name, 0) for name in names(node))
        if node.tag == "p":
            self.paragraph(node, quote, listing)
        elif node.tag in {"h4", "h5", "h6"}:
            if quote or listing:
                raise FomcParseError("unsupported_fomc_heading_context")
            self.emit(
                inline_text(node.children), BlockKind.HEADING, quote, listing, int(node.tag[1])
            )
        elif node.tag in CONTAINERS or node.tag == "li":
            self.container(node, quote, listing)
        elif node.tag == "blockquote":
            self.quote_count += 1
            self.container(node, (*quote, self.quote_count), listing)
        elif node.tag in {"ul", "ol"}:
            if node.attrs.get("type") not in {None, "1"}:
                raise FomcParseError("unsupported_fomc_list")
            position = self.list_number(node.attrs.get("start"), 1)
            for child in node.children:
                if isinstance(child, str) and not child.strip():
                    continue
                if not isinstance(child, _Node) or child.tag != "li":
                    raise FomcParseError("unsupported_fomc_list")
                position = self.list_number(child.attrs.get("value"), position)
                self.node(child, quote, (*listing, ListPosition(node.tag == "ol", position)))
                position += 1
        elif node.tag == "hr":
            self.notes_region = True
        else:
            raise FomcParseError()

    @staticmethod
    def list_number(value: str | None, default: int) -> int:
        if value is None:
            return default
        if not re.fullmatch(r"-?[0-9]{1,7}", value) or abs(int(value)) > 1000000:
            raise FomcParseError("unsupported_fomc_list")
        return int(value)


def _body(article: _Node, kind: DocumentKind) -> _Node:
    if kind == DocumentKind.MINUTES:
        return article
    divs = [n for n in article.children if isinstance(n, _Node) and n.tag == "div"]
    bodies = [
        n
        for n in divs
        if "col-sm-8" in (n.attrs.get("class") or "").split()
        and "heading" not in (n.attrs.get("class") or "").split()
    ]
    if len(bodies) != 1:
        raise FomcParseError("ambiguous_fomc_body")
    for child in article.children:
        if isinstance(child, str) and not child.strip():
            continue
        if child is bodies[0]:
            continue
        if (
            not isinstance(child, _Node)
            or child.tag != "div"
            or not ({"heading", "col-sm-4"} & set((child.attrs.get("class") or "").split()))
        ):
            raise FomcParseError("unexpected_fomc_body")
    return bodies[0]


def parse_document(
    raw: FetchedDocument, calendar: CalendarEvidence, config: ParserConfig = DEFAULT_PARSER_CONFIG
) -> ParsedDocument:
    try:
        validated = validate_document(raw, calendar)
        body = _body(article_tree(raw.body), raw.document.kind)
        if not body.complete:
            raise FomcParseError("incomplete_fomc_body")
        extractor = _Extractor(validated, config)
        extractor.container(body)
        if extractor.pending or extractor.dateline_pending:
            raise FomcParseError("dangling_fomc_metadata")
        if not any(
            b.kind in {BlockKind.PARAGRAPH, BlockKind.QUOTE, BlockKind.LIST_ITEM}
            for b in extractor.blocks
        ):
            raise FomcParseError("missing_fomc_body")
        anchors = [a.name for block in extractor.blocks for a in block.anchors]
        if len(anchors) != len(set(anchors)):
            raise FomcParseError("ambiguous_fomc_anchor")
        issues = set()
        for block in extractor.blocks:
            for ref in block.references:
                if ref.target not in anchors:
                    if ref.kind in {ReferenceKind.FOOTNOTE, ReferenceKind.BACKLINK}:
                        raise FomcParseError("unresolved_fomc_footnote")
                    issues.add("unresolved_local_link")
        return build_document(validated, tuple(extractor.blocks), config, tuple(sorted(issues)))
    except FomcValidationError, UnicodeError, RecursionError:
        raise FomcParseError("invalid_fomc_html") from None
