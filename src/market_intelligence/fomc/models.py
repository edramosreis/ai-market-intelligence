"""Stable logical identities and source dates; local receipts are a separate concern."""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

SOURCE = "federal_reserve_board/fomc"
ORIGIN = "https://www.federalreserve.gov"
CALENDAR_URL = f"{ORIGIN}/monetarypolicy/fomccalendars.htm"


class DocumentKind(StrEnum):
    STATEMENT = "statement"
    MINUTES = "minutes"


def identity(namespace: str, *parts: str) -> str:
    """Hash an unambiguous, versioned tuple; never depend on Python's process hash."""
    canonical = json.dumps([namespace, *parts], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


@dataclass(frozen=True)
class DocumentSpec:
    kind: DocumentKind
    meeting_start: date
    meeting_end: date
    published_on: date
    publication_evidence_url: str
    release_time_text: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, DocumentKind)
            or any(
                type(value) is not date
                for value in (self.meeting_start, self.meeting_end, self.published_on)
            )
            or self.meeting_start > self.meeting_end
            or self.published_on < self.meeting_end
        ):
            raise ValueError("Invalid document kind or source dates")
        expected = self.url if self.kind == DocumentKind.STATEMENT else CALENDAR_URL
        if self.publication_evidence_url != expected:
            raise ValueError("Unexpected publication evidence locator")
        if self.release_time_text is not None and (
            self.kind != DocumentKind.STATEMENT
            or not 1 <= len(self.release_time_text) <= 100
            or any(ord(char) < 32 for char in self.release_time_text)
        ):
            raise ValueError("Invalid explicit release-time text")

    @property
    def document_id(self) -> str:
        return identity("document-v1", SOURCE, self.kind.value, self.meeting_end.isoformat())

    @property
    def url(self) -> str:
        label = self.meeting_end.strftime("%Y%m%d")
        path = (
            f"/newsevents/pressreleases/monetary{label}a.htm"
            if self.kind == DocumentKind.STATEMENT
            else f"/monetarypolicy/fomcminutes{label}.htm"
        )
        return ORIGIN + path

    @property
    def publication_precision(self) -> str:
        # Explicit publisher time text remains separate; do not invent a minutes instant.
        return "date"
