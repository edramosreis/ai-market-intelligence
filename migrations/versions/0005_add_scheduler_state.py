"""Add operational scheduler state and dispatch attempts, without modifying domain data.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduler_job_state",
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("initialized_at", sa.DateTime(timezone=True)),
        sa.Column("schedule_cursor", sa.DateTime(timezone=True)),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pause_code", sa.Text()),
        sa.Column("last_dispatch_at", sa.DateTime(timezone=True)),
        sa.Column("last_completion_at", sa.DateTime(timezone=True)),
        sa.Column("next_allowed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_scheduler_job_state")),
        sa.CheckConstraint(
            "job_id IN ('coinbase', 'open_interest', 'funding', 'treasury', 'fed', 'bls')",
            name=op.f("ck_scheduler_job_state_job"),
        ),
        sa.CheckConstraint(
            "(initialized_at IS NULL AND schedule_cursor IS NULL) OR "
            "(initialized_at IS NOT NULL AND schedule_cursor IS NOT NULL "
            "AND isfinite(initialized_at) AND isfinite(schedule_cursor) "
            "AND initialized_at <= schedule_cursor)",
            name=op.f("ck_scheduler_job_state_cursor"),
        ),
        sa.CheckConstraint(
            "(NOT paused AND pause_code IS NULL) OR (paused AND pause_code IS NOT NULL "
            "AND pause_code IN ('child_failed', 'source_rejected', 'invalid_content', "
            "'launch_failed', "
            "'output_invalid', 'output_limit', 'timeout', 'stopped', 'ownership_lost', "
            "'audit_mismatch', 'recovered_unfinished', 'manual_pause'))",
            name=op.f("ck_scheduler_job_state_pause"),
        ),
        sa.CheckConstraint(
            "(last_dispatch_at IS NULL OR isfinite(last_dispatch_at)) AND "
            "(last_completion_at IS NULL OR isfinite(last_completion_at)) AND "
            "(next_allowed_at IS NULL OR (job_id = 'bls' AND isfinite(next_allowed_at) "
            "AND last_completion_at IS NOT NULL "
            "AND next_allowed_at >= last_completion_at + interval '24 hours'))",
            name=op.f("ck_scheduler_job_state_times"),
        ),
    )
    op.execute(
        "INSERT INTO scheduler_job_state (job_id) VALUES "
        "('coinbase'), ('open_interest'), ('funding'), ('treasury'), ('fed'), ('bls')"
    )
    op.create_table(
        "scheduled_job_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("definition_fingerprint", sa.Text(), nullable=False),
        sa.Column("skipped_slots", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("exit_code", sa.Integer()),
        sa.Column(
            "domain_audit_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("reconciled_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_job_runs")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["scheduler_job_state.job_id"],
            name=op.f("fk_scheduled_job_runs_job_id_scheduler_job_state"),
        ),
        sa.UniqueConstraint("job_id", "scheduled_for", name="uq_scheduled_job_runs_slot"),
        sa.CheckConstraint(
            "job_id IN ('coinbase', 'open_interest', 'funding', 'treasury', 'fed', 'bls')",
            name=op.f("ck_scheduled_job_runs_job"),
        ),
        sa.CheckConstraint(
            "isfinite(scheduled_for) AND scheduled_for >= TIMESTAMPTZ '1970-01-01 00:00:00+00' "
            "AND ((job_id = 'coinbase' AND (extract(epoch FROM scheduled_for) - 60) % 300 = 0) "
            "OR (job_id = 'open_interest' AND extract(epoch FROM scheduled_for) % 900 = 0) "
            "OR (job_id = 'funding' AND (extract(epoch FROM scheduled_for) - 300) % 3600 = 0) "
            "OR (job_id = 'treasury' AND (extract(epoch FROM scheduled_for) - 79200) % 86400 = 0) "
            "OR (job_id = 'fed' AND (extract(epoch FROM scheduled_for) - 81000) % 86400 = 0) "
            "OR (job_id = 'bls' AND (extract(epoch FROM scheduled_for) - 82800) % 86400 = 0))",
            name=op.f("ck_scheduled_job_runs_slot"),
        ),
        sa.CheckConstraint(
            "definition_fingerprint ~ '^[0-9a-f]{64}$' AND skipped_slots >= 0",
            name=op.f("ck_scheduled_job_runs_definition"),
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
            "AND error_code IN ('child_failed', 'source_rejected', 'invalid_content', "
            "'launch_failed', "
            "'output_invalid', 'output_limit', 'timeout', 'stopped', 'ownership_lost', "
            "'audit_mismatch', 'recovered_unfinished', 'manual_pause')))",
            name=op.f("ck_scheduled_job_runs_lifecycle"),
        ),
        sa.CheckConstraint(
            "exit_code IS NULL OR exit_code BETWEEN -255 AND 255",
            name=op.f("ck_scheduled_job_runs_exit"),
        ),
        sa.CheckConstraint(
            "cardinality(domain_audit_ids) <= 64 "
            "AND array_position(domain_audit_ids, NULL) IS NULL "
            "AND (cardinality(domain_audit_ids) = 0 OR array_ndims(domain_audit_ids) = 1)",
            name=op.f("ck_scheduled_job_runs_audits"),
        ),
        sa.CheckConstraint(
            "reconciled_at IS NULL OR (status = 'uncertain' AND isfinite(reconciled_at) "
            "AND reconciled_at >= finished_at)",
            name=op.f("ck_scheduled_job_runs_reconciliation"),
        ),
    )
    op.create_index(
        "uq_scheduled_job_runs_unfinished",
        "scheduled_job_runs",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_scheduled_job_runs_unfinished", table_name="scheduled_job_runs")
    op.drop_table("scheduled_job_runs")
    op.drop_table("scheduler_job_state")
