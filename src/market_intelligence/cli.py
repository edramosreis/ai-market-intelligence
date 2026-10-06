import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.candle_store import CandleStore
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.setup import grant_table_access, provision_roles
from market_intelligence.db.treasury_store import TreasuryStore
from market_intelligence.ingestion.coinbase import BASE_URL, CoinbaseClient
from market_intelligence.ingestion.models import (
    DEFAULT_START,
    ChunkReport,
    ErrorCode,
    IngestionError,
    TimeWindow,
    closed_cutoff,
    parse_instant,
    utc_now,
)
from market_intelligence.ingestion.service import ingest
from market_intelligence.treasury.client import TreasuryClient
from market_intelligence.treasury.models import (
    DATASET_CODE,
    SOURCE_CODE,
    TreasuryErrorCode,
    TreasuryIngestionError,
    TreasuryMonth,
    TreasuryReport,
    treasury_months,
)
from market_intelligence.treasury.service import ingest_treasury


def migration_config() -> Config:
    path = Path("alembic.ini")
    if not path.is_file():
        raise ValueError("Run database commands from the repository root")
    return Config(str(path))


def initialize_database(settings: DatabaseSettings) -> None:
    config = migration_config()
    provision_roles(settings)
    config.attributes["settings"] = settings
    command.upgrade(config, "head")
    grant_table_access(settings)


def report_progress(report: ChunkReport) -> None:
    print(
        json.dumps(
            {
                "event": "chunk_skipped" if report.skipped else "chunk_succeeded",
                "run_id": str(report.run_id),
                "start": report.window.start.isoformat(),
                "end": report.window.end.isoformat(),
                "expected": report.window.expected,
                "received": report.received,
                "inserted": report.inserted,
                "updated": report.updated,
                "unchanged": report.unchanged,
                "missing_buckets": report.missing_buckets,
                "coverage_basis": "stored" if report.skipped else "provider",
            }
        ),
        flush=True,
    )


def run_ingestion(args: argparse.Namespace, settings: DatabaseSettings) -> None:
    end = parse_instant(args.end) if args.end else closed_cutoff(utc_now())
    start = (
        end - timedelta(hours=72)
        if args.refresh
        else (parse_instant(args.start) if args.start else DEFAULT_START)
    )
    window = TimeWindow(start, end)
    if not 0.1 <= args.request_interval <= 60:
        raise ValueError("Request interval must be between 0.1 and 60 seconds")
    engine = create_db_engine(settings, DatabaseRole.INGEST)
    print(
        json.dumps(
            {
                "event": "ingestion_started",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "expected": window.expected,
                "resume": args.resume,
            }
        ),
        flush=True,
    )
    try:
        with httpx.Client(
            base_url=BASE_URL, headers={"User-Agent": "market-intelligence/0.1"}
        ) as http:
            reports = ingest(
                CoinbaseClient(http, request_interval=args.request_interval),
                CandleStore(engine),
                window,
                resume=args.resume,
                chunk_seconds=args.chunk_seconds,
                max_seconds=args.max_seconds,
                progress=report_progress,
            )
        print(
            json.dumps(
                {
                    "event": "ingestion_completed",
                    "chunks": len(reports),
                    "skipped_chunks": sum(report.skipped for report in reports),
                    "inserted": sum(report.inserted for report in reports),
                    "updated": sum(report.updated for report in reports),
                    "unchanged": sum(report.unchanged for report in reports),
                    "source_missing_buckets": sum(
                        r.missing_buckets for r in reports if not r.skipped
                    ),
                    "stored_missing_buckets_in_skipped_chunks": sum(
                        r.missing_buckets for r in reports if r.skipped
                    ),
                }
            ),
            flush=True,
        )
    finally:
        engine.dispose()


def report_treasury_progress(report: TreasuryReport) -> None:
    print(
        json.dumps(
            {
                "event": "treasury_month_skipped" if report.skipped else "treasury_month_succeeded",
                "source_code": SOURCE_CODE,
                "dataset_code": DATASET_CODE,
                "run_id": str(report.run_id),
                "start": report.month.start.isoformat(),
                "end": report.month.end.isoformat(),
                "received_dates": report.received_dates,
                "received_rates": report.received_rates,
                "inserted": report.inserted,
                "updated": report.updated,
                "unchanged": report.unchanged,
                "source_null": report.source_null,
                "field_absent": report.field_absent,
                "retained_dates": report.retained_dates,
                "audit_basis": "previous_validated_feed" if report.skipped else "validated_feed",
                "calendar_completeness": "not_established",
            }
        ),
        flush=True,
    )


def run_treasury_ingestion(args: argparse.Namespace, settings: DatabaseSettings) -> None:
    current = TreasuryMonth(utc_now().year, utc_now().month)
    end = date.fromisoformat(args.end) if args.end else current.end
    previous = (
        date(current.year - 1, 12, 1)
        if current.month == 1
        else date(current.year, current.month - 1, 1)
    )
    start = (
        previous
        if args.refresh
        else (date.fromisoformat(args.start) if args.start else date(2020, 1, 1))
    )
    treasury_months(start, end)  # Reject partial months before opening a database connection.
    if not 0.1 <= args.request_interval <= 60:
        raise ValueError("Request interval must be between 0.1 and 60 seconds")
    engine = create_db_engine(settings, DatabaseRole.INGEST)
    try:
        with httpx.Client(
            headers={"User-Agent": "market-intelligence/0.1"},
            follow_redirects=False,
            trust_env=False,
        ) as http:
            reports = ingest_treasury(
                TreasuryClient(http, request_interval=args.request_interval),
                TreasuryStore(engine),
                start,
                end,
                resume=args.resume,
                month_seconds=args.month_seconds,
                max_seconds=args.max_seconds,
                progress=report_treasury_progress,
            )
        fetched = [report for report in reports if not report.skipped]
        print(
            json.dumps(
                {
                    "event": "treasury_ingestion_completed",
                    "months": len(reports),
                    "skipped_months": len(reports) - len(fetched),
                    "inserted": sum(report.inserted for report in fetched),
                    "updated": sum(report.updated for report in fetched),
                    "unchanged": sum(report.unchanged for report in fetched),
                    "retained_dates": sum(report.retained_dates for report in fetched),
                    "calendar_completeness": "not_established",
                }
            ),
            flush=True,
        )
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Market intelligence database and ingestion jobs")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init-db", help="Provision local roles, migrate, and install grants")
    check = subcommands.add_parser("check-db", help="Verify connection and schema revision")
    check.add_argument("--role", choices=[role.value for role in DatabaseRole], default="read")
    server = subcommands.add_parser("serve", help="Run the read-only historical market API")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8000)
    ingestion = subcommands.add_parser(
        "ingest", help="Backfill or refresh Coinbase BTC/USD candles"
    )
    start = ingestion.add_mutually_exclusive_group()
    start.add_argument("--start", help="Aligned UTC start; defaults to 2020-01-01")
    start.add_argument("--refresh", action="store_true", help="Replay the last 72 hours")
    ingestion.add_argument("--end", help="Exclusive aligned end; defaults to closed-candle cutoff")
    ingestion.add_argument("--resume", action="store_true", help="Skip matching successful chunks")
    ingestion.add_argument(
        "--request-interval",
        type=float,
        default=0.5,
        help="Minimum seconds between request starts (default: 0.5; range: 0.1-60)",
    )
    ingestion.add_argument(
        "--chunk-seconds",
        type=float,
        default=600,
        help="Fetch/validation budget per month in seconds (default: 600)",
    )
    ingestion.add_argument("--max-seconds", type=float, help="Optional overall command deadline")
    treasury = subcommands.add_parser(
        "ingest-treasury", help="Backfill monthly nominal par yield curves"
    )
    treasury_start = treasury.add_mutually_exclusive_group()
    treasury_start.add_argument("--start", help="First-of-month DATE; defaults to 2020-01-01")
    treasury_start.add_argument(
        "--refresh", action="store_true", help="Replay previous/current months"
    )
    treasury.add_argument("--end", help="Exclusive first-of-month DATE; defaults to next month")
    treasury.add_argument(
        "--resume",
        action="store_true",
        help="Skip verified historical feed reads; always replay current month",
    )
    treasury.add_argument(
        "--request-interval", type=float, default=1, help="Request pacing seconds (0.1-60)"
    )
    treasury.add_argument(
        "--month-seconds", type=float, default=60, help="Fetch/validation budget per month"
    )
    treasury.add_argument("--max-seconds", type=float, help="Optional overall command deadline")
    args = parser.parse_args(argv)
    try:
        settings = DatabaseSettings()  # type: ignore[call-arg]
        if args.command == "init-db":
            initialize_database(settings)
            print("Database initialized; migrations and role grants applied.")
        elif args.command == "serve":
            import uvicorn

            from market_intelligence.api import create_app

            if not 1 <= args.port <= 65535:
                raise ValueError("API port must be between 1 and 65535")
            settings.credentials(DatabaseRole.READ)
            uvicorn.run(
                create_app(database_settings=settings),
                host=args.host,
                port=args.port,
                access_log=False,
            )
        elif args.command == "ingest":
            run_ingestion(args, settings)
        elif args.command == "ingest-treasury":
            run_treasury_ingestion(args, settings)
        else:
            engine = create_db_engine(settings, DatabaseRole(args.role))
            try:
                with engine.connect() as conn:
                    revision = conn.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                    market_count = conn.execute(text("SELECT count(*) FROM markets")).scalar_one()
                print(
                    f"Database ready: revision={revision}, markets={market_count}, role={args.role}"
                )
            finally:
                engine.dispose()
    except TreasuryIngestionError as treasury_error:
        print(
            json.dumps(
                {
                    "event": "treasury_ingestion_failed",
                    "code": treasury_error.code.value,
                    "run_id": str(treasury_error.run_id) if treasury_error.run_id else None,
                    "month": treasury_error.month.provider_month if treasury_error.month else None,
                    "audit_recorded": treasury_error.audit_recorded,
                    "recovery": "Rerun the same range with --resume; earlier months are retained.",
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        return 130 if treasury_error.code == TreasuryErrorCode.INTERRUPTED else 1
    except IngestionError as error:
        print(
            json.dumps(
                {
                    "event": "ingestion_failed",
                    "code": error.code.value,
                    "run_id": str(error.run_id) if error.run_id else None,
                    "start": error.window.start.isoformat() if error.window else None,
                    "end": error.window.end.isoformat() if error.window else None,
                    "audit_recorded": error.audit_recorded,
                    "recovery": (
                        "Rerun the same requested range with --resume; earlier chunks are retained."
                    ),
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        return 130 if error.code == ErrorCode.INTERRUPTED else 1
    except ValueError, RuntimeError, SQLAlchemyError, CommandError:
        parser.exit(
            1,
            "Command failed. Check configuration, provider-specific date/time bounds, "
            "service health, and migrations.\n",
        )
    return 0
