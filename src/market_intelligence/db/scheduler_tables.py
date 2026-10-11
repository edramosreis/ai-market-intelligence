"""Operational dispatch state; domain facts and coverage remain in their native tables."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from market_intelligence.db.tables import metadata

JOB_CHECK = "job_id IN ('coinbase', 'open_interest', 'funding', 'treasury', 'fed', 'bls')"
CODE_CHECK = (
    "'child_failed', 'source_rejected', 'invalid_content', 'launch_failed', "
    "'output_invalid', 'output_limit', 'timeout', 'stopped', 'ownership_lost', "
    "'audit_mismatch', 'recovered_unfinished', 'manual_pause'"
)

scheduler_job_state = sa.Table(
    "scheduler_job_state",
    metadata,
    sa.Column("job_id", sa.Text, primary_key=True),
    sa.Column("initialized_at", sa.DateTime(timezone=True)),
    sa.Column("schedule_cursor", sa.DateTime(timezone=True)),
    sa.Column("paused", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("pause_code", sa.Text),
    sa.Column("last_dispatch_at", sa.DateTime(timezone=True)),
    sa.Column("last_completion_at", sa.DateTime(timezone=True)),
    sa.Column("next_allowed_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint(JOB_CHECK, name="job"),
    sa.CheckConstraint(
        "(initialized_at IS NULL AND schedule_cursor IS NULL) OR "
        "(initialized_at IS NOT NULL AND schedule_cursor IS NOT NULL "
        "AND isfinite(initialized_at) AND isfinite(schedule_cursor) "
        "AND initialized_at <= schedule_cursor)",
        name="cursor",
    ),
    sa.CheckConstraint(
        f"(NOT paused AND pause_code IS NULL) OR (paused AND pause_code IS NOT NULL "
        f"AND pause_code IN ({CODE_CHECK}))",
        name="pause",
    ),
    sa.CheckConstraint(
        "(last_dispatch_at IS NULL OR isfinite(last_dispatch_at)) AND "
        "(last_completion_at IS NULL OR isfinite(last_completion_at)) AND "
        "(next_allowed_at IS NULL OR (job_id = 'bls' AND isfinite(next_allowed_at) "
        "AND last_completion_at IS NOT NULL "
        "AND next_allowed_at >= last_completion_at + interval '24 hours'))",
        name="times",
    ),
)

scheduled_job_runs = sa.Table(
    "scheduled_job_runs",
    metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True),
    sa.Column("owner_id", UUID(as_uuid=True), nullable=False),
    sa.Column("job_id", sa.Text, sa.ForeignKey("scheduler_job_state.job_id"), nullable=False),
    sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
    sa.Column("definition_fingerprint", sa.Text, nullable=False),
    sa.Column("skipped_slots", sa.Integer, nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("finished_at", sa.DateTime(timezone=True)),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("error_code", sa.Text),
    sa.Column("exit_code", sa.Integer),
    sa.Column(
        "domain_audit_ids",
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        server_default=sa.text("'{}'::uuid[]"),
    ),
    sa.Column("reconciled_at", sa.DateTime(timezone=True)),
    sa.UniqueConstraint("job_id", "scheduled_for", name="uq_scheduled_job_runs_slot"),
    sa.CheckConstraint(JOB_CHECK, name="job"),
    sa.CheckConstraint(
        "isfinite(scheduled_for) AND scheduled_for >= TIMESTAMPTZ '1970-01-01 00:00:00+00' "
        "AND ((job_id = 'coinbase' AND (extract(epoch FROM scheduled_for) - 60) % 300 = 0) "
        "OR (job_id = 'open_interest' AND extract(epoch FROM scheduled_for) % 900 = 0) "
        "OR (job_id = 'funding' AND (extract(epoch FROM scheduled_for) - 300) % 3600 = 0) "
        "OR (job_id = 'treasury' AND (extract(epoch FROM scheduled_for) - 79200) % 86400 = 0) "
        "OR (job_id = 'fed' AND (extract(epoch FROM scheduled_for) - 81000) % 86400 = 0) "
        "OR (job_id = 'bls' AND (extract(epoch FROM scheduled_for) - 82800) % 86400 = 0))",
        name="slot",
    ),
    sa.CheckConstraint(
        "definition_fingerprint ~ '^[0-9a-f]{64}$' AND skipped_slots >= 0", name="definition"
    ),
    sa.CheckConstraint(
        "isfinite(started_at) AND scheduled_for <= started_at AND "
        "((status = 'running' AND finished_at IS NULL AND error_code IS NULL "
        "AND exit_code IS NULL AND cardinality(domain_audit_ids) = 0) OR "
        "(status = 'succeeded' AND finished_at IS NOT NULL AND isfinite(finished_at) "
        "AND finished_at >= started_at AND error_code IS NULL AND exit_code = 0 "
        "AND cardinality(domain_audit_ids) > 0) OR "
        "(status IN ('failed', 'uncertain') AND finished_at IS NOT NULL "
        "AND isfinite(finished_at) AND finished_at >= started_at AND error_code IS NOT NULL "
        f"AND error_code IN ({CODE_CHECK})))",
        name="lifecycle",
    ),
    sa.CheckConstraint("exit_code IS NULL OR exit_code BETWEEN -255 AND 255", name="exit"),
    sa.CheckConstraint(
        "cardinality(domain_audit_ids) <= 64 AND array_position(domain_audit_ids, NULL) IS NULL "
        "AND (cardinality(domain_audit_ids) = 0 OR array_ndims(domain_audit_ids) = 1)",
        name="audits",
    ),
    sa.CheckConstraint(
        "reconciled_at IS NULL OR (status = 'uncertain' AND isfinite(reconciled_at) "
        "AND reconciled_at >= finished_at)",
        name="reconciliation",
    ),
)
sa.Index(
    "uq_scheduled_job_runs_unfinished",
    scheduled_job_runs.c.job_id,
    unique=True,
    postgresql_where=scheduled_job_runs.c.status == "running",
)
