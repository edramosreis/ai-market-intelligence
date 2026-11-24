"""Seed the remaining six scheduled 2024 FOMC statement/minutes pairs."""

import hashlib
import json
from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORIGIN = "https://www.federalreserve.gov"
CALENDAR = f"{ORIGIN}/monetarypolicy/fomccalendars.htm"
MEETINGS = (
    (date(2024, 1, 30), date(2024, 1, 31), date(2024, 2, 21), "EST"),
    (date(2024, 3, 19), date(2024, 3, 20), date(2024, 4, 10), "EDT"),
    (date(2024, 4, 30), date(2024, 5, 1), date(2024, 5, 22), "EDT"),
    (date(2024, 6, 11), date(2024, 6, 12), date(2024, 7, 3), "EDT"),
    (date(2024, 7, 30), date(2024, 7, 31), date(2024, 8, 21), "EDT"),
    (date(2024, 12, 17), date(2024, 12, 18), date(2025, 1, 8), "EST"),
)
CATALOG = sa.table(
    "fomc_documents",
    sa.column("document_id", sa.Text()),
    sa.column("kind", sa.Text()),
    sa.column("meeting_start", sa.Date()),
    sa.column("meeting_end", sa.Date()),
    sa.column("published_on", sa.Date()),
    sa.column("publication_precision", sa.Text()),
    sa.column("official_url", sa.Text()),
    sa.column("publication_evidence_url", sa.Text()),
    sa.column("explicit_release_time_text", sa.Text()),
    schema="public",
)


def _id(kind: str, end: date) -> str:
    key = ("document-v1", "federal_reserve_board/fomc", kind, end.isoformat())
    return hashlib.sha256(json.dumps(key, separators=(",", ":")).encode("ascii")).hexdigest()


def upgrade() -> None:
    rows: list[dict[str, str | date | None]] = []
    for start, end, minutes_published, zone in MEETINGS:
        statement = f"{ORIGIN}/newsevents/pressreleases/monetary{end:%Y%m%d}a.htm"
        minutes = f"{ORIGIN}/monetarypolicy/fomcminutes{end:%Y%m%d}.htm"
        common: dict[str, str | date | None] = dict(
            meeting_start=start, meeting_end=end, publication_precision="date"
        )
        rows.append(
            dict(
                common,
                document_id=_id("statement", end),
                kind="statement",
                published_on=end,
                official_url=statement,
                publication_evidence_url=statement,
                explicit_release_time_text=f"For release at 2:00 p.m. {zone}",
            )
        )
        rows.append(
            dict(
                common,
                document_id=_id("minutes", end),
                kind="minutes",
                published_on=minutes_published,
                official_url=minutes,
                publication_evidence_url=CALENDAR,
                explicit_release_time_text=None,
            )
        )
    op.bulk_insert(CATALOG, rows)


def downgrade() -> None:
    ids = [_id(kind, end) for _, end, _, _ in MEETINGS for kind in ("statement", "minutes")]
    op.execute(
        "LOCK TABLE public.fomc_ingestion_runs, public.fomc_documents IN SHARE ROW EXCLUSIVE MODE"
    )
    # Audit targets are stored in JSONB. A failed run may have no receipt FK to protect it.
    ids_sql = ", ".join(f"'{document_id}'" for document_id in ids)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.fomc_ingestion_runs
                WHERE jsonb_exists_any(requested_ids, ARRAY[{ids_sql}])
            ) THEN
                RAISE EXCEPTION 'Cannot remove 2024 FOMC catalog entries referenced by audits';
            END IF;
        END
        $$;
        """
    )
    op.execute(CATALOG.delete().where(CATALOG.c.document_id.in_(ids)))
