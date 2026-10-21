"""Small synthetic Board-shaped pages; no copied provider body or network access."""

from datetime import UTC, datetime
from uuid import uuid4

from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.client import FetchedDocument
from market_intelligence.fomc.models import CALENDAR_URL, DocumentKind, DocumentSpec

MONTHS = {9: "September", 10: "October", 11: "November"}


def page(url: str, title: str, article: str) -> bytes:
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta property="og:url" content="{url}">'
        f'<meta property="og:title" content="{title}"></head><body>'
        "<svg><title>Unrelated icon</title></svg>"
        f'<div id="article">{article}</div><div id="lastUpdate">'
        "Last Update: October 09, 2024</div></body></html>"
    ).encode()


def calendar_body() -> bytes:
    rows = []
    for statement, minutes in zip(PILOT_DOCUMENTS[::2], PILOT_DOCUMENTS[1::2], strict=True):
        rows.append(
            '<div class="fomc-meeting row" harmless="yes">'
            f"{MONTHS[minutes.meeting_end.month]} "
            f"{minutes.meeting_start.day}-{minutes.meeting_end.day}* "
            f'<a href="{statement.url}">Statement HTML</a>'
            '<div class="fomc-meeting__minutes">'
            f'<a href="{minutes.url}">Minutes HTML</a> '
            f"(Released {MONTHS[minutes.published_on.month]} "
            f"{minutes.published_on.day:02}, 2024)</div></div>"
        )
    return page(CALENDAR_URL, "FOMC calendars", "".join(rows))


def document_body(spec: DocumentSpec, extra: str = "") -> bytes:
    meeting = (
        f"{MONTHS[spec.meeting_end.month]} {spec.meeting_start.day}-{spec.meeting_end.day}, 2024"
    )
    if spec.kind == DocumentKind.STATEMENT:
        title = "Federal Reserve issues FOMC statement"
        meta = title
        labels = (
            f'<p class="article__time">{MONTHS[spec.published_on.month]} '
            f"{spec.published_on.day}, 2024</p><p>{spec.release_time_text}</p>"
        )
    else:
        title = "Minutes of the Federal Open Market Committee"
        meta = "FOMC Minutes, " + meeting
        labels = f"<p>{meeting}</p>"
    return page(
        spec.url,
        meta,
        f"<h3>{title}</h3>{labels}<p>Synthetic policy text for deterministic identity "
        f"validation. This paragraph is deliberately invented for tests.</p>{extra}",
    )


def fetched(
    spec: DocumentSpec, body: bytes | None = None, *, at: datetime | None = None
) -> FetchedDocument:
    instant = at or datetime(2026, 10, 20, tzinfo=UTC)
    body = body if body is not None else document_body(spec)
    return FetchedDocument(
        spec,
        body,
        uuid4(),
        instant,
        instant,
        "text/html; charset=utf-8",
        "identity",
        len(body),
        None,
        None,
    )
