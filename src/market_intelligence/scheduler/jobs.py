"""Fixed commands and pure UTC scheduling; no database or provider access."""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class JobId(StrEnum):
    COINBASE = "coinbase"
    OPEN_INTEREST = "open_interest"
    FUNDING = "funding"
    TREASURY = "treasury"
    FED = "fed"
    BLS = "bls"


class SchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCHEDULER_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    enabled: bool = False
    jobs: str = ""
    poll_seconds: float = Field(default=1, ge=0.1, le=30, allow_inf_nan=False)

    @field_validator("jobs")
    @classmethod
    def validate_jobs(cls, value: str) -> str:
        selected = tuple(part.strip() for part in value.split(",")) if value.strip() else ()
        if len(set(selected)) != len(selected) or any(part not in JobId for part in selected):
            raise ValueError("Select unique fixed scheduler job IDs")
        return ",".join(selected)

    @property
    def selected(self) -> tuple[JobId, ...]:
        return tuple(JobId(part) for part in self.jobs.split(",")) if self.jobs else ()


@dataclass(frozen=True)
class JobDefinition:
    id: JobId
    interval_seconds: int
    offset_seconds: int
    arguments: tuple[str, ...]
    command_seconds: int

    @property
    def fingerprint(self) -> str:
        data = [
            1,
            self.id,
            self.interval_seconds,
            self.offset_seconds,
            self.arguments,
            self.command_seconds,
            30,
            65536,
        ]
        return hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest()


DEFINITIONS = (
    JobDefinition(
        JobId.COINBASE,
        300,
        60,
        ("ingest", "--refresh", "--chunk-seconds", "60", "--max-seconds", "120"),
        120,
    ),
    JobDefinition(
        JobId.OPEN_INTEREST, 900, 0, ("collect-open-interest", "--max-seconds", "30"), 30
    ),
    JobDefinition(
        JobId.FUNDING,
        3600,
        300,
        ("ingest-funding", "--refresh", "--month-seconds", "90", "--max-seconds", "240"),
        240,
    ),
    JobDefinition(
        JobId.TREASURY,
        86400,
        79200,
        ("ingest-treasury", "--refresh", "--month-seconds", "60", "--max-seconds", "150"),
        150,
    ),
    JobDefinition(
        JobId.FED,
        86400,
        81000,
        (
            "ingest-fed",
            "--refresh",
            "--window-seconds",
            "180",
            "--max-seconds",
            "240",
            "--max-requests",
            "3",
        ),
        240,
    ),
    JobDefinition(
        JobId.BLS,
        86400,
        82800,
        (
            "ingest-bls",
            "--refresh",
            "--window-seconds",
            "180",
            "--max-seconds",
            "240",
            "--max-requests",
            "3",
        ),
        240,
    ),
)
JOBS = {definition.id: definition for definition in DEFINITIONS}
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Scheduler instants must be timezone-aware")
    return value.astimezone(UTC)


def latest_slot(job: JobDefinition, now: datetime) -> datetime:
    elapsed = (utc(now) - EPOCH).total_seconds()
    index = int((elapsed - job.offset_seconds) // job.interval_seconds)
    return EPOCH + timedelta(seconds=index * job.interval_seconds + job.offset_seconds)


def next_slot(job: JobDefinition, now: datetime) -> datetime:
    return latest_slot(job, now) + timedelta(seconds=job.interval_seconds)


@dataclass(frozen=True)
class DueSlot:
    slot: datetime
    skipped_slots: int


def due_slot(
    job: JobDefinition, cursor: datetime, now: datetime, next_allowed_at: datetime | None = None
) -> DueSlot | None:
    cursor, now = utc(cursor), utc(now)
    slot = latest_slot(job, now)
    if slot <= cursor or (next_allowed_at is not None and now < utc(next_allowed_at)):
        return None
    first = next_slot(job, cursor)
    skipped = int((slot - first).total_seconds() // job.interval_seconds)
    return DueSlot(slot, skipped)
