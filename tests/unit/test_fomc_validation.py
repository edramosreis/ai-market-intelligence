from dataclasses import replace

import pytest

from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.models import DocumentSpec
from market_intelligence.fomc.validation import (
    FomcValidationError,
    validate_calendar,
    validate_document,
)
from tests.fomc_fakes import calendar_body, document_body, fetched


@pytest.mark.parametrize("spec", PILOT_DOCUMENTS)
def test_identity_and_separate_publication_evidence(spec: DocumentSpec) -> None:
    result = validate_document(fetched(spec), validate_calendar(calendar_body()))
    assert result.fetched.document == spec
    assert result.page_update_text == "Last Update: October 09, 2024"
    assert result.validator_version == "fomc-identity-v3"
    assert result.release_time_text == spec.release_time_text


@pytest.mark.parametrize(
    "old,new",
    [
        (b"Federal Reserve issues FOMC statement", b"Access denied"),
        (b"September 18, 2024", b"September 19, 2024"),
        (b"For release at 2:00 p.m. EDT", b"For release at 3:00 p.m. EDT"),
        (b"monetary20240918a.htm", b"monetary20241107a.htm"),
        (b"</head>", b'<meta property="og:url" content="duplicate"></head>'),
        (b'</div><div id="lastUpdate">', b'<div id="lastUpdate">'),
        (b'charset="utf-8"', b'charset="utf-16"'),
        (b"<!doctype html>", b'<!DOCTYPE html SYSTEM "https://invalid/schema">'),
    ],
)
def test_reject_mismatched_or_ambiguous_identity(old: bytes, new: bytes) -> None:
    spec = PILOT_DOCUMENTS[0]
    with pytest.raises(FomcValidationError, match="^invalid_fomc_document$"):
        validate_document(
            fetched(spec, document_body(spec).replace(old, new)), validate_calendar(calendar_body())
        )


@pytest.mark.parametrize(
    "body",
    [b"", b"\xff", b"<html><head>", b"<div>" * 129, b"<br>" * 25001],
    ids=["empty", "utf8", "truncated", "depth", "nodes"],
)
def test_bounded_invalid_html(body: bytes) -> None:
    with pytest.raises(FomcValidationError):
        validate_calendar(body)


def test_calendar_dates_and_cross_links_are_required() -> None:
    raw = calendar_body()
    for old, new in [
        (b"October 09", b"October 10"),
        (b"September 17-18", b"September 16-18"),
        (b"monetary20240918a.htm", b"wrong.htm"),
        (b"</body>", raw + b"</body>"),
    ]:
        with pytest.raises(FomcValidationError):
            validate_calendar(raw.replace(old, new))


def test_minutes_cannot_use_page_update_as_publication_evidence() -> None:
    spec = PILOT_DOCUMENTS[1]
    evidence = replace(validate_calendar(calendar_body()), minutes_dates=())
    with pytest.raises(FomcValidationError):
        validate_document(fetched(spec), evidence)


def test_minutes_meeting_and_unicode_dash() -> None:
    spec = PILOT_DOCUMENTS[1]
    evidence = validate_calendar(calendar_body())
    raw = document_body(spec)
    assert validate_document(fetched(spec, raw.replace(b"17-18", "17–18".encode())), evidence)
    with pytest.raises(FomcValidationError):
        validate_document(fetched(spec, raw.replace(b"17-18", b"16-18")), evidence)


def test_script_does_not_supply_missing_identity() -> None:
    spec = PILOT_DOCUMENTS[0]
    raw = document_body(spec).replace(b'<p class="article__time">', b"<script>")
    with pytest.raises(FomcValidationError):
        validate_document(fetched(spec, raw), validate_calendar(calendar_body()))


@pytest.mark.parametrize("suffix", [b"</html>", b"</body></html>"])
def test_truncated_full_page_rejected(suffix: bytes) -> None:
    spec = PILOT_DOCUMENTS[0]
    raw = document_body(spec)
    assert raw.endswith(suffix)
    with pytest.raises(FomcValidationError):
        validate_document(fetched(spec, raw[: -len(suffix)]), validate_calendar(calendar_body()))


@pytest.mark.parametrize(
    "media",
    [
        "application/xhtml+xml",
        "text/html; charset=utf-16",
        "text/html; charset=utf-8; charset=us-ascii",
    ],
)
def test_calendar_media_type_is_checked(media: str) -> None:
    with pytest.raises(FomcValidationError):
        validate_calendar(calendar_body(), media)


@pytest.mark.parametrize("index", [0, 2])
@pytest.mark.parametrize("block", ["div", "p"])
def test_omitted_release_paragraph_end_excludes_following_share_ui(index: int, block: str) -> None:
    spec = PILOT_DOCUMENTS[index]
    body = document_body(spec).replace(
        f"<p>{spec.release_time_text}</p>".encode(),
        (
            f'<p class="releaseTime">{spec.release_time_text}'
            f'<{block} class="shareOptions">Share</{block}>'
        ).encode(),
    )
    result = validate_document(fetched(spec, body), validate_calendar(calendar_body()))
    assert result.release_time_text == spec.release_time_text


def test_implicit_end_does_not_accept_wrong_release_time() -> None:
    spec = PILOT_DOCUMENTS[0]
    body = document_body(spec).replace(
        f"<p>{spec.release_time_text}</p>".encode(),
        b'<p class="releaseTime">For release at 3:00 p.m. EDT<div>Share</div>',
    )
    with pytest.raises(FomcValidationError):
        validate_document(fetched(spec, body), validate_calendar(calendar_body()))


def test_hidden_svg_cannot_close_visible_identity_paragraph() -> None:
    from market_intelligence.fomc.validation import _Page

    parser = _Page()
    parser.feed("<p>visible<svg><div>hidden</div></svg>continued</p>")
    parser.close()
    paragraphs = [n for n in parser.root.nodes() if n.tag == "p"]
    assert len(paragraphs) == 1 and paragraphs[0].complete
    assert paragraphs[0].text() == "visible continued"


@pytest.mark.parametrize("index", [1, 3])
@pytest.mark.parametrize("inline", [False, True])
def test_minutes_dateline_shares_paragraph(index: int, inline: bool) -> None:
    spec = PILOT_DOCUMENTS[index]
    label = f"{spec.meeting_start:%B} {spec.meeting_start.day}-{spec.meeting_end.day}, 2024"
    display = label.replace("-", "–")
    content = f"<strong>{display}</strong><br>" if inline else display + " "
    body = document_body(spec).replace(
        f"<p>{label}</p>".encode(),
        f"<p>{content}Synthetic opening prose in the same paragraph.</p>".encode(),
    )
    result = validate_document(fetched(spec, body), validate_calendar(calendar_body()))
    assert result.meeting_label == display
    assert result.publication_label == f"{spec.published_on:%B} {spec.published_on.day}, 2024"


@pytest.mark.parametrize(
    "labels",
    [
        "<p>November 5-7, 2024 Opening prose mentions November 6-7, 2024.</p>",
        "<p>November 6-7, 20240 Opening prose.</p>",
        "<p>Opening prose mentions November 6-7, 2024.</p>",
        "<p>November 6-7, 2024 Opening prose.</p><p>November 6-7, 2024 Second dateline.</p>",
    ],
)
def test_minutes_dateline_remains_exact_leading_and_unambiguous(labels: str) -> None:
    spec = PILOT_DOCUMENTS[3]
    body = document_body(spec).replace(b"<p>November 6-7, 2024</p>", labels.encode())
    with pytest.raises(FomcValidationError):
        validate_document(fetched(spec, body), validate_calendar(calendar_body()))
