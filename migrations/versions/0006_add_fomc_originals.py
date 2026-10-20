"""Add immutable FOMC originals and local acquisition provenance. Earlier data is untouched."""

# Frozen PostgreSQL DDL retains exact constraint expressions.
# ruff: noqa: E501
from collections.abc import Sequence
from datetime import date
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DDL = """
CREATE TABLE fomc_documents (
	document_id TEXT NOT NULL,
	kind TEXT NOT NULL,
	meeting_start DATE NOT NULL,
	meeting_end DATE NOT NULL,
	published_on DATE NOT NULL,
	publication_precision TEXT NOT NULL,
	official_url TEXT NOT NULL,
	publication_evidence_url TEXT NOT NULL,
	explicit_release_time_text TEXT,
	CONSTRAINT pk_fomc_documents PRIMARY KEY (document_id),
	CONSTRAINT uq_fomc_documents_kind UNIQUE (kind, meeting_end),
	CONSTRAINT ck_fomc_documents_identity CHECK (document_id ~ '^[0-9a-f]{64}$' AND kind IN ('statement','minutes') AND publication_precision = 'date'),
	CONSTRAINT ck_fomc_documents_dates CHECK (isfinite(meeting_start) AND isfinite(meeting_end) AND isfinite(published_on) AND meeting_start <= meeting_end AND meeting_end <= published_on),
	CONSTRAINT ck_fomc_documents_labels CHECK (official_url ~ '^https://www[.]federalreserve[.]gov/[a-zA-Z0-9/]+[.]htm$' AND publication_evidence_url ~ '^https://www[.]federalreserve[.]gov/[a-zA-Z0-9/]+[.]htm$' AND length(official_url) <= 256 AND length(publication_evidence_url) <= 256 AND (explicit_release_time_text IS NULL OR length(explicit_release_time_text) BETWEEN 1 AND 256)),
	CONSTRAINT uq_fomc_documents_official_url UNIQUE (official_url)
);
CREATE TABLE fomc_ingestion_runs (
	run_id UUID NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	status TEXT NOT NULL,
	requested_ids JSONB NOT NULL,
	documents_completed INTEGER DEFAULT '0' NOT NULL,
	http_attempts INTEGER DEFAULT '0' NOT NULL,
	wire_bytes BIGINT DEFAULT '0' NOT NULL,
	entity_bytes BIGINT DEFAULT '0' NOT NULL,
	error_code TEXT,
	CONSTRAINT pk_fomc_ingestion_runs PRIMARY KEY (run_id),
	CONSTRAINT ck_fomc_ingestion_runs_request CHECK (jsonb_typeof(requested_ids) = 'array' AND jsonb_array_length(requested_ids)=4 AND pg_column_size(requested_ids) <= 1024),
	CONSTRAINT ck_fomc_ingestion_runs_counts CHECK (documents_completed BETWEEN 0 AND 4 AND http_attempts BETWEEN 0 AND 8 AND wire_bytes >= 0 AND entity_bytes >= 0 AND wire_bytes + entity_bytes <= 20971520),
	CONSTRAINT ck_fomc_ingestion_runs_lifecycle CHECK (isfinite(started_at) AND ((status='running' AND finished_at IS NULL AND error_code IS NULL) OR (status='succeeded' AND isfinite(finished_at) AND finished_at >= started_at AND error_code IS NULL AND documents_completed=4 AND wire_bytes+entity_bytes<=16777216) OR (status='failed' AND isfinite(finished_at) AND finished_at >= started_at AND error_code IS NOT NULL AND error_code IN ('invalid_fomc_document','artifact_error','database_error','stale_receipt','interrupted','internal_error','invalid_response','source_rejected','http_error','retry_exhausted','deadline_exceeded','request_limit','size_limit','client_halted'))) AND (status='running' OR finished_at IS NOT NULL))
);
CREATE TABLE fomc_raw_versions (
	raw_version_id TEXT NOT NULL,
	document_id TEXT NOT NULL,
	raw_sha256 TEXT NOT NULL,
	byte_count INTEGER NOT NULL,
	first_receipt_id UUID NOT NULL,
	CONSTRAINT pk_fomc_raw_versions PRIMARY KEY (raw_version_id),
	CONSTRAINT uq_fomc_raw_versions_document_id UNIQUE (document_id, raw_sha256),
	CONSTRAINT uq_fomc_raw_versions_raw_version_id UNIQUE (raw_version_id, document_id, raw_sha256, byte_count),
	CONSTRAINT ck_fomc_raw_versions_content CHECK (raw_version_id ~ '^[0-9a-f]{64}$' AND raw_sha256 ~ '^[0-9a-f]{64}$' AND byte_count BETWEEN 1 AND 2097152),
	CONSTRAINT fk_fomc_raw_versions_document_id_fomc_documents FOREIGN KEY(document_id) REFERENCES fomc_documents (document_id)
);
CREATE TABLE fomc_fetch_receipts (
	receipt_id UUID NOT NULL,
	run_id UUID NOT NULL,
	resource_kind TEXT NOT NULL,
	document_id TEXT,
	raw_version_id TEXT,
	requested_url TEXT NOT NULL,
	final_url TEXT NOT NULL,
	http_status INTEGER NOT NULL,
	content_type TEXT NOT NULL,
	content_encoding TEXT NOT NULL,
	wire_bytes INTEGER NOT NULL,
	entity_bytes INTEGER NOT NULL,
	raw_sha256 TEXT NOT NULL,
	fetch_started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	etag TEXT,
	last_modified TEXT,
	validator_version TEXT NOT NULL,
	verified_title TEXT NOT NULL,
	meeting_label TEXT,
	publication_label TEXT,
	release_time_text TEXT,
	page_update_text TEXT,
	calendar_receipt_id UUID,
	calendar_kind TEXT DEFAULT 'calendar' NOT NULL,
	CONSTRAINT pk_fomc_fetch_receipts PRIMARY KEY (receipt_id),
	CONSTRAINT uq_fomc_receipt_content UNIQUE (receipt_id, document_id, raw_version_id, raw_sha256, entity_bytes),
	CONSTRAINT uq_fomc_receipt_time UNIQUE (receipt_id, document_id, raw_version_id, fetch_started_at, received_at),
	CONSTRAINT uq_fomc_receipt_resource UNIQUE (receipt_id, resource_kind, run_id),
	CONSTRAINT fk_fomc_fetch_receipts_raw_version_id_fomc_raw_versions FOREIGN KEY(raw_version_id, document_id, raw_sha256, entity_bytes) REFERENCES fomc_raw_versions (raw_version_id, document_id, raw_sha256, byte_count),
	CONSTRAINT fk_fomc_fetch_receipts_calendar_receipt_id_fomc_fetch_receipts FOREIGN KEY(calendar_receipt_id, calendar_kind, run_id) REFERENCES fomc_fetch_receipts (receipt_id, resource_kind, run_id),
	CONSTRAINT ck_fomc_fetch_receipts_resource CHECK (calendar_kind='calendar' AND ((resource_kind='calendar' AND requested_url='https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm' AND document_id IS NULL AND raw_version_id IS NULL AND calendar_receipt_id IS NULL AND meeting_label IS NULL AND publication_label IS NULL) OR (resource_kind='document' AND document_id IS NOT NULL AND raw_version_id IS NOT NULL AND calendar_receipt_id IS NOT NULL AND meeting_label IS NOT NULL AND publication_label IS NOT NULL))),
	CONSTRAINT ck_fomc_fetch_receipts_response CHECK (http_status=200 AND requested_url=final_url AND length(requested_url)<=256 AND requested_url ~ '^https://www[.]federalreserve[.]gov/[a-zA-Z0-9/]+[.]htm$' AND content_type LIKE 'text/html%%' AND length(content_type)<=256 AND content_encoding IN ('identity','gzip') AND raw_sha256 ~ '^[0-9a-f]{64}$' AND wire_bytes BETWEEN 1 AND 2097152 AND entity_bytes BETWEEN 1 AND 2097152),
	CONSTRAINT ck_fomc_fetch_receipts_times CHECK (isfinite(fetch_started_at) AND isfinite(received_at) AND received_at>=fetch_started_at),
	CONSTRAINT ck_fomc_fetch_receipts_labels CHECK (length(validator_version) BETWEEN 1 AND 64 AND length(verified_title) BETWEEN 1 AND 256 AND (etag IS NULL OR length(etag) BETWEEN 1 AND 1024) AND (last_modified IS NULL OR length(last_modified) BETWEEN 1 AND 1024) AND (meeting_label IS NULL OR length(meeting_label) BETWEEN 1 AND 256) AND (publication_label IS NULL OR length(publication_label) BETWEEN 1 AND 256) AND (release_time_text IS NULL OR length(release_time_text) BETWEEN 1 AND 256) AND (page_update_text IS NULL OR length(page_update_text) BETWEEN 1 AND 256)),
	CONSTRAINT fk_fomc_fetch_receipts_run_id_fomc_ingestion_runs FOREIGN KEY(run_id) REFERENCES fomc_ingestion_runs (run_id),
	CONSTRAINT fk_fomc_fetch_receipts_document_id_fomc_documents FOREIGN KEY(document_id) REFERENCES fomc_documents (document_id)
);
CREATE TABLE fomc_current (
	document_id TEXT NOT NULL,
	raw_version_id TEXT NOT NULL,
	last_receipt_id UUID NOT NULL,
	last_fetch_started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_fomc_current PRIMARY KEY (document_id),
	CONSTRAINT fk_fomc_current_last_receipt_id_fomc_fetch_receipts FOREIGN KEY(last_receipt_id, document_id, raw_version_id, last_fetch_started_at, received_at) REFERENCES fomc_fetch_receipts (receipt_id, document_id, raw_version_id, fetch_started_at, received_at),
	CONSTRAINT fk_fomc_current_document_id_fomc_documents FOREIGN KEY(document_id) REFERENCES fomc_documents (document_id)
);
ALTER TABLE fomc_raw_versions ADD CONSTRAINT fk_fomc_first_receipt FOREIGN KEY(first_receipt_id, document_id, raw_version_id, raw_sha256, byte_count) REFERENCES fomc_fetch_receipts (receipt_id, document_id, raw_version_id, raw_sha256, entity_bytes) DEFERRABLE INITIALLY DEFERRED;
CREATE UNIQUE INDEX uq_fomc_running ON fomc_ingestion_runs (status) WHERE status='running';
"""

SEEDS: list[dict[str, Any]] = [
    {
        "document_id": "30b04cafff2776a946bbf83b1e6df6bf10165c682de4fc2cc44058f9447e2e5e",
        "kind": "statement",
        "meeting_start": "2024-09-17",
        "meeting_end": "2024-09-18",
        "published_on": "2024-09-18",
        "publication_precision": "date",
        "official_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20240918a.htm",
        "publication_evidence_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20240918a.htm",
        "explicit_release_time_text": "For release at 2:00 p.m. EDT",
    },
    {
        "document_id": "f4579fc68df49db16d18af5e20ae4d9f9780073a480b4d68bee7241f498610b0",
        "kind": "minutes",
        "meeting_start": "2024-09-17",
        "meeting_end": "2024-09-18",
        "published_on": "2024-10-09",
        "publication_precision": "date",
        "official_url": "https://www.federalreserve.gov/monetarypolicy/fomcminutes20240918.htm",
        "publication_evidence_url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "explicit_release_time_text": None,
    },
    {
        "document_id": "bea90fd4850b12ab006b14767fed3b289d65abd368709e58af08f28604bc584e",
        "kind": "statement",
        "meeting_start": "2024-11-06",
        "meeting_end": "2024-11-07",
        "published_on": "2024-11-07",
        "publication_precision": "date",
        "official_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20241107a.htm",
        "publication_evidence_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20241107a.htm",
        "explicit_release_time_text": "For release at 2:00 p.m. EST",
    },
    {
        "document_id": "04913dfad137cfc4f1b77390ceccd817e9ae34156cf6ec54431189114cfad69b",
        "kind": "minutes",
        "meeting_start": "2024-11-06",
        "meeting_end": "2024-11-07",
        "published_on": "2024-11-26",
        "publication_precision": "date",
        "official_url": "https://www.federalreserve.gov/monetarypolicy/fomcminutes20241107.htm",
        "publication_evidence_url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "explicit_release_time_text": None,
    },
]


def upgrade() -> None:
    op.execute(DDL)
    catalog = sa.table(
        "fomc_documents",
        *[
            sa.column(
                name,
                sa.Date()
                if name in {"meeting_start", "meeting_end", "published_on"}
                else sa.Text(),
            )
            for name in SEEDS[0]
        ],
    )
    rows = [dict(row) for row in SEEDS]
    for row in rows:
        for key in ("meeting_start", "meeting_end", "published_on"):
            row[key] = date.fromisoformat(str(row[key]))
    op.bulk_insert(catalog, rows)


def downgrade() -> None:
    op.drop_table("fomc_current")
    op.drop_constraint("fk_fomc_first_receipt", "fomc_raw_versions", type_="foreignkey")
    for table in (
        "fomc_fetch_receipts",
        "fomc_raw_versions",
        "fomc_ingestion_runs",
        "fomc_documents",
    ):
        op.drop_table(table)
