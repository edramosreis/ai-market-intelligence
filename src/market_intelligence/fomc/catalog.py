"""Reviewed HTML statements and minutes for scheduled 2024 FOMC meetings."""

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


def _pair(start: date, end: date, released: date, zone: str) -> tuple[DocumentSpec, ...]:
    return (
        DocumentSpec(
            DocumentKind.STATEMENT,
            start,
            end,
            end,
            f"{ORIGIN}/newsevents/pressreleases/monetary{end:%Y%m%d}a.htm",
            f"For release at 2:00 p.m. {zone}",
        ),
        DocumentSpec(DocumentKind.MINUTES, start, end, released, CALENDAR_URL),
    )


ADDITIONAL_BATCHES: dict[str, tuple[DocumentSpec, ...]] = {
    "2024-apr-may-dec": (
        *_pair(date(2024, 4, 30), date(2024, 5, 1), date(2024, 5, 22), "EDT"),
        *_pair(date(2024, 12, 17), date(2024, 12, 18), date(2025, 1, 8), "EST"),
    ),
    "2024-jan-mar": (
        *_pair(date(2024, 1, 30), date(2024, 1, 31), date(2024, 2, 21), "EST"),
        *_pair(date(2024, 3, 19), date(2024, 3, 20), date(2024, 4, 10), "EDT"),
    ),
    "2024-jun-jul": (
        *_pair(date(2024, 6, 11), date(2024, 6, 12), date(2024, 7, 3), "EDT"),
        *_pair(date(2024, 7, 30), date(2024, 7, 31), date(2024, 8, 21), "EDT"),
    ),
}
BATCHES = {"pilot": PILOT_DOCUMENTS, **ADDITIONAL_BATCHES}
CATALOG_DOCUMENTS = tuple(spec for batch in BATCHES.values() for spec in batch)
PILOT_IDS = tuple(spec.document_id for spec in PILOT_DOCUMENTS)
_BY_ID = {spec.document_id: spec for spec in CATALOG_DOCUMENTS}
CATALOG_IDS = tuple(_BY_ID)


def catalog_document(document_id: str) -> DocumentSpec:
    try:
        return _BY_ID[document_id]
    except KeyError:
        raise ValueError("Unknown FOMC document identity") from None


def meeting_documents(spec: DocumentSpec) -> tuple[DocumentSpec, ...]:
    return tuple(
        candidate for candidate in CATALOG_DOCUMENTS if candidate.meeting_end == spec.meeting_end
    )


def pilot_document(document_id: str) -> DocumentSpec:
    """Only server-defined identities select targets; never accept caller-supplied URLs."""
    for document in PILOT_DOCUMENTS:
        if document.document_id == document_id:
            return document
    raise ValueError("Unknown pilot document identity")
