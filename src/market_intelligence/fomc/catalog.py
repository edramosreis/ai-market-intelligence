"""Reviewed four-document pilot; this is neither discovery nor a complete FOMC calendar."""

from datetime import date

from market_intelligence.fomc.models import CALENDAR_URL, ORIGIN, DocumentKind, DocumentSpec

PILOT_DOCUMENTS: tuple[DocumentSpec, ...] = (
    DocumentSpec(
        DocumentKind.STATEMENT,
        date(2024, 9, 17),
        date(2024, 9, 18),
        date(2024, 9, 18),
        f"{ORIGIN}/newsevents/pressreleases/monetary20240918a.htm",
        "For release at 2:00 p.m. EDT",
    ),
    DocumentSpec(
        DocumentKind.MINUTES,
        date(2024, 9, 17),
        date(2024, 9, 18),
        date(2024, 10, 9),
        CALENDAR_URL,
    ),
    DocumentSpec(
        DocumentKind.STATEMENT,
        date(2024, 11, 6),
        date(2024, 11, 7),
        date(2024, 11, 7),
        f"{ORIGIN}/newsevents/pressreleases/monetary20241107a.htm",
        "For release at 2:00 p.m. EST",
    ),
    DocumentSpec(
        DocumentKind.MINUTES,
        date(2024, 11, 6),
        date(2024, 11, 7),
        date(2024, 11, 26),
        CALENDAR_URL,
    ),
)


def pilot_document(document_id: str) -> DocumentSpec:
    """Only server-defined identities select targets; never accept caller-supplied URLs."""
    for document in PILOT_DOCUMENTS:
        if document.document_id == document_id:
            return document
    raise ValueError("Unknown pilot document identity")
