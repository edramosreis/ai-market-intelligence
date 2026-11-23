"""Explicit FOMC processing and reader-only status commands."""

from argparse import ArgumentParser, Namespace
from dataclasses import asdict
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.fomc_processing_store import FomcProcessingStore
from market_intelligence.fomc.artifacts import ArtifactStore
from market_intelligence.fomc.catalog import ADDITIONAL_BATCHES
from market_intelligence.fomc.chunking import DEFAULT_CHUNKER_CONFIG
from market_intelligence.fomc.cli import ARTIFACT_ROOT, private_artifact_root, report
from market_intelligence.fomc.isolation import require_fomc_database
from market_intelligence.fomc.parsing import DEFAULT_PARSER_CONFIG
from market_intelligence.fomc.processing import process_batch, process_pilot
from market_intelligence.fomc.processing_models import ProcessingError


def add_arguments(parser: ArgumentParser) -> None:
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--pilot", action="store_true", help="Process the four stored originals")
    operation.add_argument(
        "--batch", choices=ADDITIONAL_BATCHES, help="Process one stored 2024 batch"
    )
    operation.add_argument("--status", action="store_true", help="Read passage readiness")
    parser.add_argument(
        "--artifact-root", type=Path, help="Private stored originals for processing"
    )


def execute(args: Namespace) -> int:
    try:
        if args.status and args.artifact_root is not None:
            raise ValueError("Artifact root applies only to processing")
        settings = DatabaseSettings(_env_file=None)  # type: ignore[call-arg]
        role = DatabaseRole.READ if args.status else DatabaseRole.INGEST
        require_fomc_database(settings.url(role))
        root = None if args.status else private_artifact_root(args.artifact_root or ARTIFACT_ROOT)
        engine = create_db_engine(settings, role)
        try:
            store = FomcProcessingStore(engine)
            if root is None:
                report(
                    dict(
                        event="fomc_processing_status",
                        parser_config_sha256=DEFAULT_PARSER_CONFIG.config_hash,
                        chunker_config_sha256=DEFAULT_CHUNKER_CONFIG.config_hash,
                        documents=store.status(DEFAULT_PARSER_CONFIG, DEFAULT_CHUNKER_CONFIG),
                    )
                )
                return 0
            artifacts = ArtifactStore(root)
            result = (
                process_pilot(store, artifacts)
                if args.pilot
                else process_batch(store, artifacts, args.batch)
            )
            report(dict(event="fomc_processing_completed", **asdict(result)))
            return 0 if all(d.outcome in {"created", "reused"} for d in result.documents) else 1
        finally:
            engine.dispose()
    except ProcessingError as error:
        report(dict(event="fomc_processing_failed", code=error.code))
    except ValueError, SQLAlchemyError, OSError, RuntimeError:
        report(dict(event="fomc_processing_failed", code="configuration_or_database_error"))
    except KeyboardInterrupt:
        report(dict(event="fomc_processing_failed", code="interrupted"))
        return 130
    return 1
