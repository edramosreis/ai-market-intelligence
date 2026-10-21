"""Opt-in isolated pilot and read-only run status; no crawl, scheduler or model access."""

import argparse
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.fomc_store import FomcStore, FomcStoreError
from market_intelligence.fomc.artifacts import ArtifactStore
from market_intelligence.fomc.client import FomcAcquisitionClient
from market_intelligence.fomc.isolation import require_fomc_database
from market_intelligence.fomc.service import FomcIngestionError, ingest_pilot
from market_intelligence.time_utils import utc_now

ARTIFACT_ROOT = Path("/app/data/fomc")


def private_artifact_root(value: Path) -> Path:
    root = value.absolute()
    cwd = Path.cwd()
    runtime = cwd == Path("/app") and root == ARTIFACT_ROOT
    linked = (cwd / ".git").is_file() and not (cwd / ".git").is_symlink()
    if not runtime and not (linked and root == cwd / "data/fomc"):
        raise ValueError("Use the private M3 artifact directory")
    return root


def add_arguments(parser: argparse.ArgumentParser) -> None:
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument(
        "--pilot", action="store_true", help="Acquire only the four reviewed originals"
    )
    operation.add_argument(
        "--status", action="store_true", help="Read the latest twenty run audits"
    )
    operation.add_argument(
        "--reconcile-run",
        type=UUID,
        help="Mark an inspected interrupted run failed; stop its process first",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        choices=range(1, 9),
        default=8,
        help="HTTP attempts including retries, shared by calendar/documents (1-8)",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=ARTIFACT_ROOT,
        help="Private content-addressed original storage",
    )


def json_value(value: object) -> str:
    if isinstance(value, (UUID, date, datetime)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    raise TypeError("Unsupported FOMC report value")


def report(value: dict[str, object]) -> None:
    print(json.dumps(value, default=json_value), flush=True)


def execute(args: argparse.Namespace) -> int:
    try:
        if not args.pilot and args.artifact_root != ARTIFACT_ROOT:
            raise ValueError("Artifact root applies only to the pilot")
        # FOMC never loads the original demo dotenv file implicitly.
        settings = DatabaseSettings(_env_file=None)  # type: ignore[call-arg]
        role = DatabaseRole.READ if args.status else DatabaseRole.INGEST
        require_fomc_database(settings.url(role))
        engine = create_db_engine(settings, role)
        try:
            store = FomcStore(engine)
            if args.status:
                report(dict(event="fomc_status", runs=store.status()))
            elif args.reconcile_run:
                store.reconcile(args.reconcile_run, utc_now())
                report(dict(event="fomc_run_reconciled", run_id=args.reconcile_run))
            else:
                artifacts = ArtifactStore(private_artifact_root(args.artifact_root))
                with httpx.Client(trust_env=False, follow_redirects=False) as http:
                    result = ingest_pilot(
                        store,
                        FomcAcquisitionClient(http, max_requests=args.max_requests),
                        artifacts,
                    )
                report(dict(event="fomc_ingestion_completed", **asdict(result)))
        finally:
            engine.dispose()
        return 0
    except FomcIngestionError as error:
        report(
            dict(
                event="fomc_ingestion_failed",
                code=error.code,
                run_id=error.run_id,
                audit_recorded=error.audit_recorded,
                unreferenced_artifact_possible=error.unreferenced_artifact_possible,
                recovery=(
                    "Inspect run status and retained artifacts; "
                    "reconcile an interrupted run before rerunning."
                ),
            )
        )
        return 130 if error.code == "interrupted" else 1
    except FomcStoreError as error:
        report(dict(event="fomc_command_failed", code=error.code))
    except ValueError, SQLAlchemyError, OSError, RuntimeError:
        report(dict(event="fomc_command_failed", code="configuration_or_database_error"))
    return 1
