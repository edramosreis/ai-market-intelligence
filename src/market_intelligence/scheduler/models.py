"""Controlled operational outcomes, independent of native data coverage."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class OutcomeCode(StrEnum):
    CHILD_FAILED = "child_failed"
    SOURCE_REJECTED = "source_rejected"
    INVALID_CONTENT = "invalid_content"
    LAUNCH_FAILED = "launch_failed"
    OUTPUT_INVALID = "output_invalid"
    OUTPUT_LIMIT = "output_limit"
    TIMEOUT = "timeout"
    STOPPED = "stopped"
    OWNERSHIP_LOST = "ownership_lost"
    AUDIT_MISMATCH = "audit_mismatch"
    RECOVERED_UNFINISHED = "recovered_unfinished"
    MANUAL_PAUSE = "manual_pause"


@dataclass(frozen=True)
class AuditReference:
    id: UUID
    status: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, UUID) or self.status not in {"succeeded", "failed"}:
            raise ValueError("Invalid native audit reference")


@dataclass(frozen=True)
class ChildOutcome:
    status: str
    code: OutcomeCode | None = None
    exit_code: int | None = None
    audits: tuple[AuditReference, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "uncertain"}:
            raise ValueError("Invalid child outcome")
        if (self.status == "succeeded") != (self.code is None):
            raise ValueError("Invalid outcome code")
        if self.exit_code is not None and (
            type(self.exit_code) is not int or not -255 <= self.exit_code <= 255
        ):
            raise ValueError("Invalid exit code")
        if len(self.audits) > 64 or len({audit.id for audit in self.audits}) != len(self.audits):
            raise ValueError("Invalid audit list")

    @property
    def pause(self) -> bool:
        return self.status == "uncertain" or self.code in {
            OutcomeCode.SOURCE_REJECTED,
            OutcomeCode.INVALID_CONTENT,
            OutcomeCode.LAUNCH_FAILED,
        }
