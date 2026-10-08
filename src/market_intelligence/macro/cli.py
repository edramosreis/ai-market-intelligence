"""Fixed provider commands, pre-configuration validation and sanitized progress."""

import argparse
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.macro_store import MacroStore
from market_intelligence.ingestion.models import utc_now
from market_intelligence.macro.bls import BlsClient
from market_intelligence.macro.fed import FedClient
from market_intelligence.macro.jobs import (
    HISTORY_START,
    MacroIngestionError,
    expected_periods,
    job_windows,
    refresh_start,
)
from market_intelligence.macro.models import MacroProvider, MonthlyWindow, utc
from market_intelligence.macro.service import ingest_macro
from market_intelligence.macro.storage_models import MacroWriteReport


@dataclass(frozen=True)
class MacroCommand:
    provider: MacroProvider
    window: MonthlyWindow
    resume: bool
    request_interval: float
    window_seconds: float
    max_seconds: float
    max_requests: int


def add_arguments(parser: argparse.ArgumentParser, provider: MacroProvider) -> None:
    start = parser.add_mutually_exclusive_group()
    start.add_argument(
        "--start", help=f"First-of-month DATE; defaults to {HISTORY_START[provider]}"
    )
    start.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "Replay current year and preceding five years"
            if provider == MacroProvider.BLS
            else "Replay full native monthly history with one release read"
        ),
    )
    parser.add_argument("--end", help="Exclusive first-of-month DATE; defaults to current month")
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Reuse complete older reads; always replay the five-year revision region"
            if provider == MacroProvider.BLS
            else "Reuse complete older explicit windows; always refetch the latest completed month"
        ),
    )
    parser.add_argument(
        "--request-interval", type=float, default=3, help="Request pacing seconds (3-60)"
    )
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=180,
        help="Fetch/validation budget per request window",
    )
    parser.add_argument(
        "--max-seconds", type=float, default=900, help="Overall job budget (default 900 seconds)"
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=12 if provider == MacroProvider.BLS else 3,
        help="Per-command HTTP attempt limit including retries (1-25); not a daily quota guard",
    )


def _month(raw: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-01", raw):
        raise ValueError("Expected first-of-month YYYY-MM-01")
    return date.fromisoformat(raw)


def command_plan(args: argparse.Namespace, observed_now: datetime) -> MacroCommand:
    provider = MacroProvider.BLS if args.command == "ingest-bls" else MacroProvider.FED
    if args.refresh and (args.resume or args.end):
        raise ValueError("Macro refresh cannot be combined with resume or explicit end")
    today = utc(observed_now).date()
    end = _month(args.end) if args.end else today.replace(day=1)
    start = (
        refresh_start(provider, today)
        if args.refresh
        else (_month(args.start) if args.start else HISTORY_START[provider])
    )
    window = MonthlyWindow(start, end)
    job_windows(provider, window, observed_now)
    if not math.isfinite(args.request_interval) or not 3 <= args.request_interval <= 60:
        raise ValueError("Macro request pacing must be between three and sixty seconds")
    if any(
        not math.isfinite(seconds) or seconds <= 0
        for seconds in (args.window_seconds, args.max_seconds)
    ):
        raise ValueError("Macro job budgets must be finite positive seconds")
    if not 1 <= args.max_requests <= 25:
        raise ValueError("Macro request limit must be between one and twenty-five")
    return MacroCommand(
        provider,
        window,
        args.resume,
        args.request_interval,
        args.window_seconds,
        args.max_seconds,
        args.max_requests,
    )


def progress(report: MacroWriteReport) -> None:
    print(
        json.dumps(
            {
                "event": "macro_window_skipped" if report.skipped else "macro_window_succeeded",
                "provider": report.provider.value,
                "run_id": str(report.run_id),
                "start": report.window.start.isoformat(),
                "end": report.window.end.isoformat(),
                "received_periods": report.received,
                "absent_from_read": len(expected_periods(report.provider, report.window))
                - report.received,
                "source_missing": report.source_missing,
                "retained": report.retained,
                "inserted": 0 if report.skipped else report.inserted,
                "corrected": 0 if report.skipped else report.corrected,
                "unchanged": 0 if report.skipped else report.unchanged,
                "annual_average_exclusions": report.annual_average_count,
                "audit_basis": "previous_complete_native_month_read"
                if report.skipped
                else "validated_read",
                "publication_calendar_completeness": "not_established",
            }
        ),
        flush=True,
    )


def run(plan: MacroCommand, settings: DatabaseSettings) -> None:
    engine = create_db_engine(settings, DatabaseRole.INGEST)
    try:
        with httpx.Client(
            headers={"User-Agent": "market-intelligence/0.1"},
            follow_redirects=False,
            trust_env=False,
        ) as http:
            client_type = BlsClient if plan.provider == MacroProvider.BLS else FedClient
            source = client_type(
                http,
                request_interval=plan.request_interval,
                max_requests=plan.max_requests,
                now=utc_now,
            )
            print(
                json.dumps(
                    {
                        "event": "macro_ingestion_started",
                        "provider": plan.provider.value,
                        "start": plan.window.start.isoformat(),
                        "end": plan.window.end.isoformat(),
                        "resume": plan.resume,
                        "max_requests": plan.max_requests,
                    }
                ),
                flush=True,
            )
            try:
                reports = ingest_macro(
                    source,
                    MacroStore(engine),
                    plan.provider,
                    plan.window,
                    resume=plan.resume,
                    window_seconds=plan.window_seconds,
                    max_seconds=plan.max_seconds,
                    now=utc_now,
                    progress=progress,
                )
            except MacroIngestionError as error:
                error.requests_used = source.requests_used
                error.request_limit = plan.max_requests
                raise
            fetched = [report for report in reports if not report.skipped]
            print(
                json.dumps(
                    {
                        "event": "macro_ingestion_completed",
                        "provider": plan.provider.value,
                        "windows": len(reports),
                        "skipped_windows": len(reports) - len(fetched),
                        "requests_used": source.requests_used,
                        "inserted": sum(report.inserted for report in fetched),
                        "corrected": sum(report.corrected for report in fetched),
                        "unchanged": sum(report.unchanged for report in fetched),
                        "retained": sum(report.retained for report in fetched),
                        "source_missing": sum(report.source_missing for report in fetched),
                        "absent_from_reads": sum(
                            len(expected_periods(plan.provider, report.window)) - report.received
                            for report in fetched
                        ),
                        "publication_calendar_completeness": "not_established",
                    }
                ),
                flush=True,
            )
    finally:
        engine.dispose()


def failure_report(error: MacroIngestionError) -> dict[str, object]:
    command = "ingest-bls" if error.provider == MacroProvider.BLS else "ingest-fed"
    recovery = (
        f"Retry {command} --start {error.window.start.isoformat()} "
        f"--end {error.window.end.isoformat()} without --resume; then rerun the original "
        "range with --resume. Earlier committed windows are retained."
        if error.window
        else "Rerun the original command; earlier committed windows are retained."
    )
    return {
        "event": "macro_ingestion_failed",
        "provider": error.provider.value,
        "code": error.code.value,
        "storage_code": error.storage_code.value if error.storage_code else None,
        "run_id": str(error.run_id) if error.run_id else None,
        "start": error.window.start.isoformat() if error.window else None,
        "end": error.window.end.isoformat() if error.window else None,
        "audit_recorded": error.audit_recorded,
        "requests_used": error.requests_used,
        "request_limit": error.request_limit,
        "request_budget_exhausted": error.request_limit is not None
        and error.requests_used >= error.request_limit,
        "recovery": recovery,
    }
