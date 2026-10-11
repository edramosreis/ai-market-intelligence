"""Configuration preview, reader status, writer controls and explicitly guarded dispatch."""

import argparse
import json
import signal
from datetime import datetime
from threading import Event
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.scheduler_store import LeadershipLost, SchedulerBusy, SchedulerStore
from market_intelligence.ingestion.models import utc_now
from market_intelligence.scheduler.jobs import DEFINITIONS, JobId, SchedulerSettings, next_slot
from market_intelligence.scheduler.process import ProcessRunner, require_runtime
from market_intelligence.scheduler.service import SchedulerService


def add_arguments(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="schedule_command", required=True)
    commands.add_parser(
        "preview", help="Show fixed jobs and future UTC slots without database access"
    )
    status = commands.add_parser(
        "status", help="Read state and latest attempts using reader credentials"
    )
    status.add_argument("--limit", type=int, default=20)
    run = commands.add_parser(
        "run", help="Run explicitly enabled jobs in the Linux runtime container"
    )
    run.add_argument(
        "--once", action="store_true", help="One admission pass, including first enrollment"
    )
    for name in ("pause", "resume"):
        control = commands.add_parser(
            name, help=f"{name.title()} a fixed job without provider calls"
        )
        control.add_argument("job", choices=[job.value for job in JobId])
        if name == "resume":
            control.add_argument(
                "--reconcile-run",
                type=UUID,
                help="Acknowledge an inspected uncertain scheduler run; preserve its outcome",
            )


def json_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError("Unsupported scheduler output")


def report(value: dict[str, object]) -> None:
    print(json.dumps(value, default=json_value), flush=True)


def execute(args: argparse.Namespace) -> int:
    try:
        config = SchedulerSettings()
        if args.schedule_command == "preview":
            now = utc_now()
            report(
                {
                    "event": "scheduler_preview",
                    "enabled": config.enabled,
                    "observed_at": now,
                    "jobs": [
                        {
                            "job_id": job.id.value,
                            "selected": job.id in config.selected,
                            "next_nominal_slot": next_slot(job, now),
                            "arguments": job.arguments,
                            "supervisor_seconds": job.command_seconds + 30,
                            "definition_fingerprint": job.fingerprint,
                        }
                        for job in DEFINITIONS
                    ],
                }
            )
            return 0
        if args.schedule_command == "run" and (not config.enabled or not config.selected):
            report({"event": "scheduler_command_failed", "code": "disabled_or_no_jobs"})
            return 1
        if args.schedule_command == "status" and not 1 <= args.limit <= 100:
            raise ValueError("Invalid status limit")
        database = DatabaseSettings()  # type: ignore[call-arg]
        if args.schedule_command == "run":
            require_runtime(database)
        role = DatabaseRole.READ if args.schedule_command == "status" else DatabaseRole.INGEST
        engine = create_db_engine(database, role)
        try:
            store = SchedulerStore(engine)
            if args.schedule_command == "status":
                report({"event": "scheduler_status", **store.status(args.limit)})
            elif args.schedule_command == "pause":
                store.pause(JobId(args.job))
                report({"event": "scheduler_paused", "job_id": args.job})
            elif args.schedule_command == "resume":
                store.resume(JobId(args.job), utc_now(), args.reconcile_run)
                report({"event": "scheduler_resumed", "job_id": args.job})
            else:
                stop = Event()
                previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

                def sleep(seconds: float) -> None:
                    stop.wait(seconds)

                try:
                    for sig in previous:
                        signal.signal(sig, lambda _signal, _frame: stop.set())
                    SchedulerService(
                        store,
                        ProcessRunner(),
                        database,
                        config,
                        stopped=stop.is_set,
                        sleep=sleep,
                        progress=report,
                    ).run(once=args.once)
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)
        finally:
            engine.dispose()
        return 0
    except SchedulerBusy:
        code = "scheduler_busy"
    except LeadershipLost:
        code = "ownership_lost"
    except ValueError, RuntimeError, SQLAlchemyError:
        code = "configuration_or_state"
    report({"event": "scheduler_command_failed", "code": code})
    return 1
