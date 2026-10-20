"""Narrow Board identity checks, separate from later passage extraction and retrieval."""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin

from market_intelligence.fomc.catalog import PILOT_DOCUMENTS, pilot_document
from market_intelligence.fomc.client import MAX_RESPONSE_BYTES, FetchedDocument
from market_intelligence.fomc.models import CALENDAR_URL, ORIGIN, DocumentKind, DocumentSpec

VALIDATOR_VERSION = "fomc-identity-v1"
MONTHS = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
EXCLUDED = {"script", "style", "template", "noscript", "svg"}


class FomcValidationError(ValueError):
    def __init__(self) -> None:
        super().__init__("invalid_fomc_document")


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None]
    children: list[_Node | str] = field(default_factory=list)
    complete: bool = False

    def nodes(self) -> list[_Node]:
        return [
            self,
            *(
                node
                for child in self.children
                if isinstance(child, _Node)
                for node in child.nodes()
            ),
        ]

    def text(self) -> str:
        if self.tag in EXCLUDED:
            return ""
        return " ".join(
            " ".join(
                child.text() if isinstance(child, _Node) else child for child in self.children
            ).split()
        )


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]
        self.count, self.characters = 0, 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.count += 1
        if self.count > 25000 or len(self.stack) >= 128:
            raise FomcValidationError()
        relevant = [
            (name, value)
            for name, value in attrs
            if name in {"id", "class", "href", "property", "content", "charset"}
        ]
        if len({name for name, _ in relevant}) != len(relevant):
            raise FomcValidationError()
        node = _Node(tag, dict(relevant), complete=tag in VOID_TAGS)
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack[index].complete = True
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.characters += len(data)
        if self.characters > MAX_RESPONSE_BYTES:
            raise FomcValidationError()
        self.stack[-1].children.append(data)

    def handle_decl(self, decl: str) -> None:
        if decl.strip().lower() != "doctype html":
            raise FomcValidationError()

    def unknown_decl(self, data: str) -> None:
        raise FomcValidationError()


def _one(nodes: list[_Node]) -> _Node:
    if len(nodes) != 1 or not nodes[0].complete:
        raise FomcValidationError()
    return nodes[0]


def _label(node: _Node, limit: int = 256) -> str:
    value = node.text()
    if not value or len(value) > limit:
        raise FomcValidationError()
    return value


def _dash(value: str) -> str:
    return re.sub(r"[\u2010-\u2015\u2212]", "-", value)


def _page(body: bytes, expected_url: str) -> tuple[_Node, _Node, list[_Node]]:
    if not 1 <= len(body) <= MAX_RESPONSE_BYTES:
        raise FomcValidationError()
    try:
        page = _Page()
        page.feed(body.decode("utf-8-sig"))
        page.close()
    except UnicodeError, RecursionError:
        raise FomcValidationError() from None
    nodes = page.root.nodes()
    head = _one([node for node in nodes if node.tag == "head"])
    article = _one([node for node in nodes if node.attrs.get("id") == "article"])
    url = _one(
        [
            node
            for node in head.nodes()
            if node.tag == "meta" and node.attrs.get("property") == "og:url"
        ]
    )
    if url.attrs.get("content") != expected_url or not article.text():
        raise FomcValidationError()
    for node in head.nodes():
        if (
            node.tag == "meta"
            and "charset" in node.attrs
            and (node.attrs["charset"] or "").lower() not in {"utf-8", "utf8", "us-ascii"}
        ):
            raise FomcValidationError()
    return head, article, nodes


def _date(value: str) -> date:
    match = re.fullmatch(r"([A-Za-z]+) (\d{1,2}), (\d{4})", value)
    if match is None or match[1] not in MONTHS[1:]:
        raise FomcValidationError()
    try:
        return date(int(match[3]), MONTHS.index(match[1]), int(match[2]))
    except ValueError:
        raise FomcValidationError() from None


def _meeting(spec: DocumentSpec, *, year: bool = True) -> str:
    if spec.meeting_start.month != spec.meeting_end.month:
        raise FomcValidationError()
    result = f"{MONTHS[spec.meeting_start.month]} {spec.meeting_start.day}-{spec.meeting_end.day}"
    return result + (f", {spec.meeting_end.year}" if year else "")


@dataclass(frozen=True)
class CalendarEvidence:
    raw_sha256: str
    minutes_dates: tuple[tuple[str, date], ...]


def validate_calendar(body: bytes) -> CalendarEvidence:
    _, article, _ = _page(body, CALENDAR_URL)
    rows = [
        node
        for node in article.nodes()
        if "fomc-meeting" in (node.attrs.get("class") or "").split()
    ]
    result = []
    for minutes in (spec for spec in PILOT_DOCUMENTS if spec.kind == DocumentKind.MINUTES):
        statement = next(
            spec
            for spec in PILOT_DOCUMENTS
            if spec.kind == DocumentKind.STATEMENT and spec.meeting_end == minutes.meeting_end
        )
        matching = []
        for row in rows:
            links = [
                urljoin(ORIGIN, node.attrs["href"] or "")
                for node in row.nodes()
                if node.tag == "a" and "href" in node.attrs
            ]
            if minutes.url in links:
                if links.count(minutes.url) != 1 or links.count(statement.url) != 1:
                    raise FomcValidationError()
                matching.append(row)
        row = _one(matching)
        if not _dash(row.text()).startswith(_meeting(minutes, year=False)):
            raise FomcValidationError()
        section = _one(
            [
                node
                for node in row.nodes()
                if "fomc-meeting__minutes" in (node.attrs.get("class") or "").split()
            ]
        )
        labels = re.findall(r"\(Released ([A-Za-z]+ \d{1,2}, \d{4})\)", section.text())
        if len(labels) != 1 or _date(labels[0]) != minutes.published_on:
            raise FomcValidationError()
        result.append((minutes.document_id, _date(labels[0])))
    return CalendarEvidence(hashlib.sha256(body).hexdigest(), tuple(result))


@dataclass(frozen=True)
class ValidatedDocument:
    fetched: FetchedDocument
    title: str
    meeting_label: str
    publication_label: str
    release_time_text: str | None
    page_update_text: str | None
    calendar_sha256: str
    validator_version: str = VALIDATOR_VERSION


def validate_document(fetched: FetchedDocument, calendar: CalendarEvidence) -> ValidatedDocument:
    spec = fetched.document
    if pilot_document(spec.document_id) != spec:
        raise FomcValidationError()
    charset = re.search(r"charset\s*=\s*[\"']?([^\s;\"']+)", fetched.content_type, re.IGNORECASE)
    if charset is not None and charset[1].lower() not in {"utf-8", "utf8", "us-ascii"}:
        raise FomcValidationError()
    head, article, nodes = _page(fetched.body, spec.url)
    title = _label(_one([node for node in article.nodes() if node.tag == "h3"]))
    expected_title = (
        "Federal Reserve issues FOMC statement"
        if spec.kind == DocumentKind.STATEMENT
        else "Minutes of the Federal Open Market Committee"
    )
    if title != expected_title:
        raise FomcValidationError()
    meta = _one(
        [
            node
            for node in head.nodes()
            if node.tag == "meta" and node.attrs.get("property") == "og:title"
        ]
    )
    expected_meta = (
        title if spec.kind == DocumentKind.STATEMENT else "FOMC Minutes, " + _meeting(spec)
    )
    if _dash(meta.attrs.get("content") or "") != expected_meta:
        raise FomcValidationError()
    paragraphs = [
        node for node in article.nodes() if node.tag == "p" and node.complete and node.text()
    ]
    if len(paragraphs) < 2 or len(article.text()) < 80:
        raise FomcValidationError()
    meeting, release = _meeting(spec), None
    if spec.kind == DocumentKind.STATEMENT:
        published = _label(
            _one(
                [
                    node
                    for node in paragraphs
                    if "article__time" in (node.attrs.get("class") or "").split()
                ]
            )
        )
        if _date(published) != spec.published_on:
            raise FomcValidationError()
        release = _label(
            _one([node for node in paragraphs if node.text().startswith("For release at ")])
        )
        if release != spec.release_time_text:
            raise FomcValidationError()
    else:
        meeting = _label(
            _one([node for node in paragraphs if _dash(node.text()) == _meeting(spec)])
        )
        if dict(calendar.minutes_dates).get(spec.document_id) != spec.published_on:
            raise FomcValidationError()
        published = (
            f"{MONTHS[spec.published_on.month]} {spec.published_on.day}, {spec.published_on.year}"
        )
    updates = [node for node in nodes if node.attrs.get("id") == "lastUpdate"]
    update = _label(_one(updates)) if updates else None
    return ValidatedDocument(
        fetched, title, meeting, published, release, update, calendar.raw_sha256
    )
