"""One leader, one sequential child, and no transaction across child execution."""

import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID, uuid4

from market_intelligence.config import DatabaseSettings
from market_intelligence.db.scheduler_store import Leader, SchedulerStore
from market_intelligence.ingestion.models import utc_now
from market_intelligence.scheduler.jobs import DEFINITIONS, JobDefinition, SchedulerSettings
from market_intelligence.scheduler.models import ChildOutcome


class Runner(Protocol):
    def run(
        self,
        job: JobDefinition,
        settings: DatabaseSettings,
        healthy: Callable[[], bool],
        stopped: Callable[[], bool],
    ) -> ChildOutcome: ...


class SchedulerService:
    def __init__(
        self,
        store: SchedulerStore,
        runner: Runner,
        database: DatabaseSettings,
        settings: SchedulerSettings,
        *,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
        stopped: Callable[[], bool] = lambda: False,
        progress: Callable[[dict[str, object]], None] = lambda _: None,
    ) -> None:
        if not settings.enabled or not settings.selected:
            raise ValueError("Scheduler dispatch requires enablement and selected jobs")
        self.store, self.runner, self.database, self.settings = store, runner, database, settings
        self.now, self.sleep, self.stopped, self.progress = now, sleep, stopped, progress

    def run(self, *, once: bool = False) -> None:
        owner = uuid4()
        with self.store.leader() as leader:
            recovered = self.store.recover(self.now())
            self.progress({"event": "scheduler_started", "recovered_uncertain_runs": recovered})
            while not self.stopped():
                self.cycle(leader, owner)
                if once or self.stopped():
                    break
                self.sleep(self.settings.poll_seconds)

    def cycle(self, leader: Leader, owner: UUID) -> None:
        for job in DEFINITIONS:
            if job.id not in self.settings.selected or self.stopped():
                continue
            leader.require()
            attempt = self.store.admit(job.id, owner, self.now())
            if attempt is None:
                continue
            # The running intent is already committed. Any crash from here is recoverable
            # as uncertain, including the pre-launch gap; never assume it was not executed.
            leader.require()
            self.progress(
                {"event": "scheduler_dispatched", "job_id": job.id.value, "run_id": str(attempt.id)}
            )
            outcome = self.runner.run(job, self.database, leader.healthy, self.stopped)
            leader.require()
            final = self.store.finish(attempt, outcome, self.now())
            self.progress(
                {
                    "event": "scheduler_finished",
                    "job_id": job.id.value,
                    "run_id": str(attempt.id),
                    "status": final.status,
                    "code": final.code.value if final.code else None,
                    "native_audit_count": len(final.audits),
                }
            )
