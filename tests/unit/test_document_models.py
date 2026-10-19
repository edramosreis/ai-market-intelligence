"""Synthetic identity checks; no provider, artifact store or database access."""

import hashlib
from dataclasses import replace
from datetime import date, datetime

import pytest

from market_intelligence.documents.fomc import PILOT_DOCUMENTS, pilot_document
from market_intelligence.documents.models import CALENDAR_URL, DocumentKind, DocumentSpec


def test_four_selected_documents_keep_meeting_and_publication_dates_separate() -> None:
    assert len(PILOT_DOCUMENTS) == len({row.document_id for row in PILOT_DOCUMENTS}) == 4
    assert [row.url for row in PILOT_DOCUMENTS] == [
        "https://www.federalreserve.gov/newsevents/pressreleases/monetary20240918a.htm",
        "https://www.federalreserve.gov/monetarypolicy/fomcminutes20240918.htm",
        "https://www.federalreserve.gov/newsevents/pressreleases/monetary20241107a.htm",
        "https://www.federalreserve.gov/monetarypolicy/fomcminutes20241107.htm",
    ]
    minutes = [row for row in PILOT_DOCUMENTS if row.kind == DocumentKind.MINUTES]
    assert [row.published_on for row in minutes] == [date(2024, 10, 9), date(2024, 11, 26)]
    assert all(row.published_on > row.meeting_end for row in minutes)
    assert all(row.publication_evidence_url == CALENDAR_URL for row in minutes)
    assert all(
        row.release_time_text is None and row.publication_precision == "date" for row in minutes
    )
    assert PILOT_DOCUMENTS[0].release_time_text == "For release at 2:00 p.m. EDT"
    assert PILOT_DOCUMENTS[2].release_time_text == "For release at 2:00 p.m. EST"
    assert all(pilot_document(row.document_id) is row for row in PILOT_DOCUMENTS)


def test_logical_id_has_fixed_namespaced_encoding_and_excludes_publication_metadata() -> None:
    original = PILOT_DOCUMENTS[0]
    canonical = b'["document-v1","federal_reserve_board/fomc","statement","2024-09-18"]'
    assert original.document_id == hashlib.sha256(canonical).hexdigest()
    assert replace(original, published_on=date(2024, 9, 19)).document_id == original.document_id
    assert replace(original, release_time_text=None).document_id == original.document_id
    assert PILOT_DOCUMENTS[1].document_id != original.document_id
    assert PILOT_DOCUMENTS[2].document_id != original.document_id


@pytest.mark.parametrize("value", ["", "https://other.test/policy", "statement", "x" * 64])
def test_lookup_rejects_unknown_ids_and_arbitrary_urls(value: str) -> None:
    with pytest.raises(ValueError, match="Unknown pilot document identity"):
        pilot_document(value)


@pytest.mark.parametrize(
    "start,end,published,evidence,release",
    [
        (date(2024, 9, 19), date(2024, 9, 18), date(2024, 10, 9), CALENDAR_URL, None),
        (datetime(2024, 9, 17), date(2024, 9, 18), date(2024, 10, 9), CALENDAR_URL, None),
        (date(2024, 9, 17), date(2024, 9, 18), date(2024, 9, 17), CALENDAR_URL, None),
        (date(2024, 9, 17), date(2024, 9, 18), date(2024, 10, 9), "https://other.test", None),
        (date(2024, 9, 17), date(2024, 9, 18), date(2024, 10, 9), CALENDAR_URL, "2 p.m."),
    ],
)
def test_invalid_source_dates_and_evidence_rejected(
    start: date, end: date, published: date, evidence: str, release: str | None
) -> None:
    with pytest.raises(ValueError):
        DocumentSpec(DocumentKind.MINUTES, start, end, published, evidence, release)
