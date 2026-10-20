"""FOMC original-document provenance, separate from later parsing and retrieval."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from market_intelligence.db.tables import metadata


def define_tables(meta: sa.MetaData) -> dict[str, sa.Table]:
    documents = sa.Table(
        "fomc_documents",
        meta,
        sa.Column("document_id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("meeting_start", sa.Date, nullable=False),
        sa.Column("meeting_end", sa.Date, nullable=False),
        sa.Column("published_on", sa.Date, nullable=False),
        sa.Column("publication_precision", sa.Text, nullable=False),
        sa.Column("official_url", sa.Text, nullable=False, unique=True),
        sa.Column("publication_evidence_url", sa.Text, nullable=False),
        sa.Column("explicit_release_time_text", sa.Text),
        sa.UniqueConstraint("kind", "meeting_end"),
        sa.CheckConstraint(
            "document_id ~ '^[0-9a-f]{64}$' AND kind IN ('statement','minutes') AND "
            "publication_precision = 'date'",
            name="identity",
        ),
        sa.CheckConstraint(
            "isfinite(meeting_start) AND isfinite(meeting_end) AND "
            "isfinite(published_on) AND meeting_start <= meeting_end AND "
            "meeting_end <= published_on",
            name="dates",
        ),
        sa.CheckConstraint(
            "official_url ~ '^https://www[.]federalreserve[.]gov/[a-zA-Z0-9/]+[.]htm$' "
            "AND publication_evidence_url ~ "
            "'^https://www[.]federalreserve[.]gov/[a-zA-Z0-9/]+[.]htm$' "
            "AND length(official_url) <= 256 AND length(publication_evidence_url) <= 256 "
            "AND (explicit_release_time_text IS NULL OR "
            "length(explicit_release_time_text) BETWEEN 1 AND 256)",
            name="labels",
        ),
    )
    runs = sa.Table(
        "fomc_ingestion_runs",
        meta,
        sa.Column("run_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("requested_ids", JSONB, nullable=False),
        sa.Column("documents_completed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("http_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("wire_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("entity_bytes", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text),
        sa.CheckConstraint(
            "jsonb_typeof(requested_ids) = 'array' AND jsonb_array_length(requested_ids)=4 "
            "AND pg_column_size(requested_ids) <= 1024",
            name="request",
        ),
        sa.CheckConstraint(
            "documents_completed BETWEEN 0 AND 4 AND http_attempts BETWEEN 0 AND 8 "
            "AND wire_bytes >= 0 AND entity_bytes >= 0 AND "
            "wire_bytes + entity_bytes <= 20971520",
            name="counts",
        ),
        sa.CheckConstraint(
            "isfinite(started_at) AND ((status='running' AND finished_at IS NULL "
            "AND error_code IS NULL) "
            "OR (status='succeeded' AND isfinite(finished_at) AND finished_at >= started_at "
            "AND error_code IS NULL AND documents_completed=4 AND "
            "wire_bytes+entity_bytes<=16777216) "
            "OR (status='failed' AND isfinite(finished_at) AND finished_at >= started_at AND "
            "error_code IS NOT NULL AND error_code IN "
            "('invalid_fomc_document','artifact_error','database_error',"
            "'stale_receipt','interrupted','internal_error','invalid_response','source_rejected',"
            "'http_error','retry_exhausted','deadline_exceeded','request_limit','size_limit','client_halted')))"
            " "
            "AND (status='running' OR finished_at IS NOT NULL)",
            name="lifecycle",
        ),
        sa.Index(
            "uq_fomc_running", "status", unique=True, postgresql_where=sa.text("status='running'")
        ),
    )
    versions = sa.Table(
        "fomc_raw_versions",
        meta,
        sa.Column("raw_version_id", sa.Text, primary_key=True),
        sa.Column("document_id", sa.Text, sa.ForeignKey(documents.c.document_id), nullable=False),
        sa.Column("raw_sha256", sa.Text, nullable=False),
        sa.Column("byte_count", sa.Integer, nullable=False),
        sa.Column("first_receipt_id", UUID(as_uuid=True), nullable=False),
        sa.UniqueConstraint("document_id", "raw_sha256"),
        sa.UniqueConstraint("raw_version_id", "document_id", "raw_sha256", "byte_count"),
        sa.CheckConstraint(
            "raw_version_id ~ '^[0-9a-f]{64}$' AND raw_sha256 ~ '^[0-9a-f]{64}$' "
            "AND byte_count BETWEEN 1 AND 2097152",
            name="content",
        ),
    )
    receipts = sa.Table(
        "fomc_fetch_receipts",
        meta,
        sa.Column("receipt_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey(runs.c.run_id), nullable=False),
        sa.Column("resource_kind", sa.Text, nullable=False),
        sa.Column("document_id", sa.Text, sa.ForeignKey(documents.c.document_id)),
        sa.Column("raw_version_id", sa.Text),
        sa.Column("requested_url", sa.Text, nullable=False),
        sa.Column("final_url", sa.Text, nullable=False),
        sa.Column("http_status", sa.Integer, nullable=False),
        sa.Column("content_type", sa.Text, nullable=False),
        sa.Column("content_encoding", sa.Text, nullable=False),
        sa.Column("wire_bytes", sa.Integer, nullable=False),
        sa.Column("entity_bytes", sa.Integer, nullable=False),
        sa.Column("raw_sha256", sa.Text, nullable=False),
        sa.Column("fetch_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("etag", sa.Text),
        sa.Column("last_modified", sa.Text),
        sa.Column("validator_version", sa.Text, nullable=False),
        sa.Column("verified_title", sa.Text, nullable=False),
        sa.Column("meeting_label", sa.Text),
        sa.Column("publication_label", sa.Text),
        sa.Column("release_time_text", sa.Text),
        sa.Column("page_update_text", sa.Text),
        sa.Column("calendar_receipt_id", UUID(as_uuid=True)),
        sa.Column("calendar_kind", sa.Text, nullable=False, server_default="calendar"),
        sa.UniqueConstraint(
            "receipt_id",
            "document_id",
            "raw_version_id",
            "raw_sha256",
            "entity_bytes",
            name="uq_fomc_receipt_content",
        ),
        sa.UniqueConstraint(
            "receipt_id",
            "document_id",
            "raw_version_id",
            "fetch_started_at",
            "received_at",
            name="uq_fomc_receipt_time",
        ),
        sa.UniqueConstraint(
            "receipt_id", "resource_kind", "run_id", name="uq_fomc_receipt_resource"
        ),
        sa.ForeignKeyConstraint(
            ["raw_version_id", "document_id", "raw_sha256", "entity_bytes"],
            [
                "fomc_raw_versions.raw_version_id",
                "fomc_raw_versions.document_id",
                "fomc_raw_versions.raw_sha256",
                "fomc_raw_versions.byte_count",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["calendar_receipt_id", "calendar_kind", "run_id"],
            [
                "fomc_fetch_receipts.receipt_id",
                "fomc_fetch_receipts.resource_kind",
                "fomc_fetch_receipts.run_id",
            ],
        ),
        sa.CheckConstraint(
            "calendar_kind='calendar' AND ((resource_kind='calendar' AND requested_url="
            "'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm' AND "
            "document_id IS NULL "
            "AND raw_version_id IS NULL AND calendar_receipt_id IS NULL AND meeting_label IS NULL "
            "AND publication_label IS NULL) OR (resource_kind='document' AND "
            "document_id IS NOT NULL "
            "AND raw_version_id IS NOT NULL AND calendar_receipt_id IS NOT NULL "
            "AND meeting_label IS NOT NULL AND publication_label IS NOT NULL))",
            name="resource",
        ),
        sa.CheckConstraint(
            "http_status=200 AND requested_url=final_url AND length(requested_url)<=256 "
            "AND requested_url ~ '^https://www[.]federalreserve[.]gov/[a-zA-Z0-9/]+[.]htm$' "
            "AND content_type LIKE 'text/html%' AND length(content_type)<=256 "
            "AND content_encoding IN ('identity','gzip') AND raw_sha256 ~ '^[0-9a-f]{64}$' "
            "AND wire_bytes BETWEEN 1 AND 2097152 AND entity_bytes BETWEEN 1 AND 2097152",
            name="response",
        ),
        sa.CheckConstraint(
            "isfinite(fetch_started_at) AND isfinite(received_at) AND "
            "received_at>=fetch_started_at",
            name="times",
        ),
        sa.CheckConstraint(
            "length(validator_version) BETWEEN 1 AND 64 AND length(verified_title) "
            "BETWEEN 1 AND 256 "
            "AND (etag IS NULL OR length(etag) BETWEEN 1 AND 1024) "
            "AND (last_modified IS NULL OR length(last_modified) BETWEEN 1 AND 1024) "
            "AND (meeting_label IS NULL OR length(meeting_label) BETWEEN 1 AND 256) "
            "AND (publication_label IS NULL OR length(publication_label) BETWEEN 1 AND 256) "
            "AND (release_time_text IS NULL OR length(release_time_text) BETWEEN 1 AND 256) "
            "AND (page_update_text IS NULL OR length(page_update_text) BETWEEN 1 AND 256)",
            name="labels",
        ),
    )
    versions.append_constraint(
        sa.ForeignKeyConstraint(
            ["first_receipt_id", "document_id", "raw_version_id", "raw_sha256", "byte_count"],
            [
                "fomc_fetch_receipts.receipt_id",
                "fomc_fetch_receipts.document_id",
                "fomc_fetch_receipts.raw_version_id",
                "fomc_fetch_receipts.raw_sha256",
                "fomc_fetch_receipts.entity_bytes",
            ],
            name="fk_fomc_first_receipt",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        )
    )
    current = sa.Table(
        "fomc_current",
        meta,
        sa.Column("document_id", sa.Text, sa.ForeignKey(documents.c.document_id), primary_key=True),
        sa.Column("raw_version_id", sa.Text, nullable=False),
        sa.Column("last_receipt_id", UUID(as_uuid=True), nullable=False),
        sa.Column("last_fetch_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            [
                "last_receipt_id",
                "document_id",
                "raw_version_id",
                "last_fetch_started_at",
                "received_at",
            ],
            [
                "fomc_fetch_receipts.receipt_id",
                "fomc_fetch_receipts.document_id",
                "fomc_fetch_receipts.raw_version_id",
                "fomc_fetch_receipts.fetch_started_at",
                "fomc_fetch_receipts.received_at",
            ],
        ),
    )
    return {table.name: table for table in (documents, runs, versions, receipts, current)}


TABLES = define_tables(metadata)
fomc_documents = TABLES["fomc_documents"]
fomc_ingestion_runs = TABLES["fomc_ingestion_runs"]
fomc_raw_versions = TABLES["fomc_raw_versions"]
fomc_fetch_receipts = TABLES["fomc_fetch_receipts"]
fomc_current = TABLES["fomc_current"]
