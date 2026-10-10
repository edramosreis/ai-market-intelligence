"""Linux child supervision with bounded streaming output and controlled event evidence."""

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import UUID

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.scheduler.jobs import JobDefinition, JobId
from market_intelligence.scheduler.models import AuditReference, ChildOutcome, OutcomeCode

OUTPUT_BYTES = 65536
LINE_BYTES = 8192
EVENTS = {
    JobId.COINBASE: (
        "ingestion_started",
        "chunk_succeeded",
        "ingestion_completed",
        "ingestion_failed",
    ),
    JobId.OPEN_INTEREST: (None, None, "open_interest_collected", "hyperliquid_ingestion_failed"),
    JobId.FUNDING: (
        None,
        "funding_window_succeeded",
        "funding_ingestion_completed",
        "hyperliquid_ingestion_failed",
    ),
    JobId.TREASURY: (
        None,
        "treasury_month_succeeded",
        "treasury_ingestion_completed",
        "treasury_ingestion_failed",
    ),
    JobId.FED: (
        "macro_ingestion_started",
        "macro_window_succeeded",
        "macro_ingestion_completed",
        "macro_ingestion_failed",
    ),
    JobId.BLS: (
        "macro_ingestion_started",
        "macro_window_succeeded",
        "macro_ingestion_completed",
        "macro_ingestion_failed",
    ),
}
FAILURE_CODES = {
    "invalid_payload",
    "invalid_candle",
    "product_mismatch",
    "source_rejected",
    "http_error",
    "retry_exhausted",
    "deadline_exceeded",
    "database_error",
    "interrupted",
    "concurrent_job",
    "internal_error",
    "stale_read",
}
COUNT_FIELDS = {
    "chunks",
    "skipped_chunks",
    "months",
    "skipped_months",
    "windows",
    "skipped_windows",
    "received",
    "received_dates",
    "received_rates",
    "expected",
    "expected_hours",
    "inserted",
    "updated",
    "corrected",
    "unchanged",
    "retained",
    "retained_dates",
    "source_null",
    "field_absent",
    "source_missing",
    "absent_from_reads",
    "requests_used",
    "max_requests",
    "request_limit",
    "annual_average_exclusions",
    "missing_buckets",
    "stored_missing_hours",
    "source_missing_buckets",
    "stored_missing_buckets_in_skipped_chunks",
    "received_periods",
    "absent_from_read",
}


def minimal_environment(settings: DatabaseSettings) -> dict[str, str]:
    user, password = settings.credentials(DatabaseRole.INGEST)
    return {
        "POSTGRES_HOST": settings.host,
        "POSTGRES_PORT": str(settings.port),
        "POSTGRES_DB": settings.db,
        "POSTGRES_INGEST_USER": user,
        "POSTGRES_INGEST_PASSWORD": password.get_secret_value(),
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
    }


def require_runtime(settings: DatabaseSettings) -> None:
    if (
        sys.platform != "linux"
        or not Path("/.dockerenv").is_file()
        or Path.cwd() != Path("/app")
        or Path(".env").exists()
    ):
        raise ValueError("Active scheduler requires the Linux runtime container without dotenv")
    if any(
        (settings.admin_user, settings.admin_password, settings.read_user, settings.read_password)
    ):
        raise ValueError("Active scheduler requires only writer credentials")
    if any(value for key, value in os.environ.items() if key.startswith(("OPENAI_", "AGENT_"))):
        raise ValueError("Active scheduler must not receive model settings")
    settings.credentials(DatabaseRole.INGEST)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate event key")
        result[key] = value
    return result


class EventCollector:
    """Keep UUIDs and controlled outcomes only; discard source details and raw text."""

    def __init__(self, job: JobId) -> None:
        self.job = job
        self.audits: list[AuditReference] = []
        self.completed = False
        self.failed = False
        self.started = False
        self.failure_code: str | None = None
        self.failure_audited = False
        self.unrecorded_attempt = False

    def line(self, data: bytes, *, stderr: bool) -> None:
        if len(data) > LINE_BYTES:
            raise ValueError("Oversized child event")
        content = data.decode("utf-8", errors="strict").strip()
        if not content:
            return
        if stderr and not content.startswith("{"):
            return  # Bounded library warnings have no operational evidence.
        row = json.loads(content, object_pairs_hook=unique_object)
        if not isinstance(row, dict):
            raise ValueError("Invalid child event")
        started, progress, completed, failed = EVENTS[self.job]
        event = row.get("event")
        if event not in {e for e in EVENTS[self.job] if e is not None} or stderr != (
            event == failed
        ):
            raise ValueError("Unexpected child event")
        if self.job in {JobId.BLS, JobId.FED} and row.get("provider") != (
            "bls" if self.job == JobId.BLS else "federal_reserve_board"
        ):
            raise ValueError("Unexpected provider")
        if (
            event == failed
            and self.job in {JobId.FUNDING, JobId.OPEN_INTEREST}
            and row.get("operation")
            != ("funding" if self.job == JobId.FUNDING else "open_interest")
        ):
            raise ValueError("Unexpected operation")
        for key in COUNT_FIELDS & row.keys():
            value = row[key]
            if value is not None and (type(value) is not int or not 0 <= value <= 2147483647):
                raise ValueError("Invalid child count")
            if (
                key in {"requests_used", "max_requests", "request_limit"}
                and value is not None
                and value > 3
            ):
                raise ValueError("Unexpected request count")
        if event == started:
            if self.started or self.completed:
                raise ValueError("Duplicate child start")
            self.started = True
        elif event == progress:
            if self.completed:
                raise ValueError("Progress after completion")
            self._audit(row, "succeeded")
        elif event == completed:
            if self.completed:
                raise ValueError("Duplicate completion")
            self.completed = True
            if self.job == JobId.OPEN_INTEREST:
                self._audit(row, "succeeded")
                UUID(row["snapshot_id"])
        else:
            if (
                self.failed
                or row.get("code") not in FAILURE_CODES
                or type(row.get("audit_recorded")) is not bool
            ):
                raise ValueError("Invalid child failure")
            self.failed = True
            self.failure_code = row["code"]
            self.failure_audited = row["audit_recorded"]
            if self.failure_audited:
                self._audit(row, "failed")
            else:
                self.unrecorded_attempt = row.get("run_id") is not None

    def _audit(self, row: dict[str, Any], status: str) -> None:
        value = row.get("run_id")
        if not isinstance(value, str):
            raise ValueError("Missing native audit UUID")
        reference = AuditReference(UUID(value), status)
        if reference.id in {a.id for a in self.audits} or len(self.audits) >= 64:
            raise ValueError("Duplicate or excessive native audits")
        self.audits.append(reference)

    def outcome(self, exit_code: int) -> ChildOutcome:
        audits = tuple(self.audits)
        if exit_code == 0 and self.completed and not self.failed and audits:
            return ChildOutcome("succeeded", exit_code=0, audits=audits)
        if (
            exit_code in {1, 130}
            and self.failed
            and not self.completed
            and not self.unrecorded_attempt
        ):
            if self.failure_code == "source_rejected":
                code = OutcomeCode.SOURCE_REJECTED
            elif self.failure_code in {
                "invalid_payload",
                "invalid_candle",
                "product_mismatch",
                "stale_read",
            }:
                code = OutcomeCode.INVALID_CONTENT
            elif self.failure_code in {"internal_error", "interrupted", "database_error"}:
                return ChildOutcome("uncertain", OutcomeCode.CHILD_FAILED, exit_code, audits)
            else:
                code = OutcomeCode.CHILD_FAILED
            return ChildOutcome("failed", code, exit_code, audits)
        return ChildOutcome(
            "uncertain",
            OutcomeCode.OUTPUT_INVALID,
            exit_code if -255 <= exit_code <= 255 else None,
            audits,
        )


class ProcessRunner:
    def run(
        self,
        job: JobDefinition,
        settings: DatabaseSettings,
        healthy: Callable[[], bool],
        stopped: Callable[[], bool],
    ) -> ChildOutcome:
        return self._supervise(
            (sys.executable, "-m", "market_intelligence", *job.arguments),
            minimal_environment(settings),
            job.id,
            job.command_seconds + 30,
            healthy,
            stopped,
        )

    def _supervise(
        self,
        arguments: tuple[str, ...],
        environment: dict[str, str],
        job: JobId,
        seconds: float,
        healthy: Callable[[], bool],
        stopped: Callable[[], bool],
        grace_seconds: float = 5,
    ) -> ChildOutcome:
        if sys.platform != "linux":
            raise ValueError("Process supervision requires Linux")
        try:
            process = subprocess.Popen(
                arguments,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                cwd="/app",
                start_new_session=True,
                close_fds=True,
            )
        except OSError:
            return ChildOutcome("failed", OutcomeCode.LAUNCH_FAILED)
        collector = EventCollector(job)
        deadline = time.monotonic() + seconds
        total = 0
        buffers: dict[int, bytes] = {}
        reason: OutcomeCode | None = None
        try:
            with selectors.DefaultSelector() as selector:
                for stream, stderr in ((process.stdout, False), (process.stderr, True)):
                    assert stream is not None
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, stderr)
                    buffers[stream.fileno()] = b""
                while selector.get_map():
                    if stopped():
                        reason = OutcomeCode.STOPPED
                        break
                    if not healthy():
                        reason = OutcomeCode.OWNERSHIP_LOST
                        break
                    if time.monotonic() >= deadline:
                        reason = OutcomeCode.TIMEOUT
                        break
                    for key, _ in selector.select(
                        timeout=min(0.25, max(0, deadline - time.monotonic()))
                    ):
                        chunk = os.read(key.fd, 4096)
                        if not chunk:
                            if buffers[key.fd]:
                                collector.line(buffers[key.fd], stderr=key.data)
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > OUTPUT_BYTES:
                            reason = OutcomeCode.OUTPUT_LIMIT
                            break
                        lines = (buffers[key.fd] + chunk).split(b"\n")
                        buffers[key.fd] = lines.pop()
                        for line in lines:
                            collector.line(line, stderr=key.data)
                        if len(buffers[key.fd]) > LINE_BYTES:
                            raise ValueError("Oversized event line")
                    if reason is not None:
                        break
            if reason is None:
                try:
                    exit_code = process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    reason = OutcomeCode.TIMEOUT
                else:
                    return collector.outcome(exit_code)
        except ValueError, UnicodeError, KeyError, TypeError:
            reason = OutcomeCode.OUTPUT_INVALID
        except KeyboardInterrupt:
            reason = OutcomeCode.STOPPED
        finally:
            # Reap the process group even when its parent closed pipes or failed parsing.
            self._terminate(process, grace_seconds)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        return ChildOutcome(
            "uncertain",
            reason or OutcomeCode.OUTPUT_INVALID,
            process.returncode
            if process.returncode is not None and -255 <= process.returncode <= 255
            else None,
            tuple(collector.audits),
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
        # killpg also handles a descendant retaining a pipe after the parent has exited.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGINT)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=grace_seconds)
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=grace_seconds)
