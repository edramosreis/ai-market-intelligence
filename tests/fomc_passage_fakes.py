"""Invented policy bodies with the two reviewed Board layout shapes."""

from market_intelligence.fomc.models import DocumentKind, DocumentSpec
from tests.fomc_fakes import page


def passage_body(spec: DocumentSpec, content: str, *, shared_date: bool = False) -> bytes:
    title = (
        "Federal Reserve issues FOMC statement"
        if spec.kind == DocumentKind.STATEMENT
        else "Minutes of the Federal Open Market Committee"
    )
    meeting = (
        f"{spec.meeting_start:%B} {spec.meeting_start.day}-"
        f"{spec.meeting_end.day}, {spec.meeting_end.year}"
    )
    if spec.kind == DocumentKind.STATEMENT:
        article = (
            '<div class="heading col-sm-8"><p class="article__time">'
            f"{spec.published_on:%B} {spec.published_on.day}, {spec.published_on.year}</p>"
            f"<h3>{title}</h3><p>{spec.release_time_text}</p>"
            '<div class="shareOptions">Share</div></div>'
            f'<div class="col-sm-8">{content}</div>'
            '<div class="col-sm-4">Unrelated sidebar</div>'
        )
        meta = title
    else:
        date = f"<p><strong>{meeting}</strong>"
        date += (
            " A synthetic opening paragraph shared with the date.</p>" if shared_date else "</p>"
        )
        article = f"<h3>{title}</h3>{date}{content}"
        meta = "FOMC Minutes, " + meeting
    return page(spec.url, meta, article)
