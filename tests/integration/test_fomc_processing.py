from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.pool import QueuePool

from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.connection import create_db_engine
from market_intelligence.db.fomc_processing_store import FomcProcessingStore
from market_intelligence.db.fomc_processing_tables import (
    PROCESSING_TABLE_NAMES,
    fomc_parsed_blocks,
    fomc_passage_sets,
    fomc_passage_spans,
    fomc_passages,
    fomc_processing_attempts,
)
from market_intelligence.db.fomc_store import FomcStore
from market_intelligence.db.tables import metadata
from market_intelligence.fomc.artifacts import ArtifactStore
from market_intelligence.fomc.catalog import PILOT_DOCUMENTS
from market_intelligence.fomc.chunking import DEFAULT_CHUNKER_CONFIG, ChunkerConfig
from market_intelligence.fomc.parsing import DEFAULT_PARSER_CONFIG
from market_intelligence.fomc.processing import process_original, process_pilot
from market_intelligence.fomc.validation import validate_calendar, validate_document
from market_intelligence.time_utils import utc_now
from tests.fomc_fakes import fetched
from tests.fomc_passage_fakes import passage_body
from tests.integration.test_fomc_store import NOW, STATS, start, store  # noqa: F401

pytestmark = pytest.mark.integration
SPEC = PILOT_DOCUMENTS[0]
FIRST = "Synthetic example describes a stable operating environment and includes 2.5 percent."
SECOND = "Second invented paragraph contains an adjacent Unicode sign at −3 percent."


def collect_one(
    acquisition: FomcStore, artifacts: ArtifactStore, day: int = 0, suffix: str = ""
) -> str:
    at = NOW + timedelta(days=day)
    run, calendar = start(acquisition, artifacts, at)
    raw = fetched(SPEC, passage_body(SPEC, f"<p>{FIRST}</p><p>{SECOND}{suffix}</p>"), at=at)
    artifacts.put(raw.body)
    acquisition.record_document(
        run,
        validate_document(raw, validate_calendar(calendar.body)),
        calendar.receipt_id,
        artifacts,
        STATS,
    )
    acquisition.finish(run, at, STATS, "interrupted")
    return raw.raw_version_id


def counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            name: conn.execute(
                sa.select(sa.func.count()).select_from(metadata.tables[name])
            ).scalar_one()
            for name in PROCESSING_TABLE_NAMES
        }


def test_store_and_reader_preserve_exact_spans_reuse_and_config_versions(
    store: FomcStore,  # noqa: F811
    database_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    writer = FomcProcessingStore(store.engine)
    pool = writer.engine.pool

    class CheckedArtifacts(ArtifactStore):
        def read_verified(self, digest: str, size: int) -> bytes:
            assert isinstance(pool, QueuePool)
            assert pool.checkedout() == 0
            return super().read_verified(digest, size)

    artifacts = CheckedArtifacts(tmp_path)
    reader_engine = create_db_engine(database_settings, DatabaseRole.READ)
    try:
        reader = FomcProcessingStore(reader_engine)
        parser, chunker = DEFAULT_PARSER_CONFIG, DEFAULT_CHUNKER_CONFIG
        assert reader.status(parser, chunker)[0]["state"] == "not_collected"
        raw_id = collect_one(store, artifacts)
        assert reader.status(parser, chunker)[0]["state"] == "pending"
        before_commit: list[str] = []

        def observe_before_commit(
            conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
        ) -> None:
            if statement.startswith("INSERT INTO fomc_processing_attempts"):
                before_commit.append(reader.status(parser, chunker)[0]["state"])

        sa.event.listen(writer.engine, "after_cursor_execute", observe_before_commit)
        try:
            run = process_pilot(writer, artifacts)
        finally:
            sa.event.remove(writer.engine, "after_cursor_execute", observe_before_commit)
        result = run.documents[0]
        assert before_commit == ["pending"]
        assert result.outcome == "created" and result.raw_version_id == raw_id
        assert result.block_count == 2 and result.passage_count == 1
        assert all(item.outcome == "not_collected" for item in run.documents[1:])
        assert reader.status(parser, chunker)[0]["state"] == "ready"
        with reader_engine.connect() as conn:
            assert conn.execute(sa.select(fomc_passages.c.body)).scalar_one() == (
                FIRST + "\n\n" + SECOND
            )
            assert conn.execute(
                sa.select(
                    fomc_passage_spans.c.block_ordinal,
                    fomc_passage_spans.c.start_offset,
                    fomc_passage_spans.c.end_offset,
                ).order_by(fomc_passage_spans.c.span_ordinal)
            ).all() == [
                (0, 0, len(FIRST)),
                (1, 0, len(SECOND)),
            ]
        before = counts(store.engine)
        assert collect_one(store, artifacts, day=1) == raw_id
        assert reader.status(parser, chunker)[0]["passage_set_id"] == result.passage_set_id
        replay = process_pilot(writer, artifacts).documents[0]
        assert replay.outcome == "reused" and replay.passage_set_id == result.passage_set_id
        assert counts(store.engine) == before | {"fomc_processing_attempts": 2}
        smaller = ChunkerConfig(target_chars=100)
        assert reader.status(parser, smaller)[0]["state"] == "pending"
        alternate = process_pilot(writer, artifacts, chunker=smaller).documents[0]
        assert alternate.outcome == "created" and alternate.passage_count == 2
        current = counts(store.engine)
        assert current["fomc_parse_generations"] == 1
        assert current["fomc_passage_sets"] == 2
    finally:
        reader_engine.dispose()


def test_acquisition_refresh_prevents_stale_publication(
    store: FomcStore,  # noqa: F811
    tmp_path: Path,
) -> None:
    writer, artifacts = FomcProcessingStore(store.engine), ArtifactStore(tmp_path)
    parser, chunker = DEFAULT_PARSER_CONFIG, DEFAULT_CHUNKER_CONFIG
    old = collect_one(store, artifacts)
    source = writer.inputs()[SPEC.document_id]
    output = process_original(source, artifacts, parser, chunker)
    assert process_pilot(writer, artifacts).documents[0].outcome == "created"
    new = collect_one(store, artifacts, day=1, suffix=" New language.")
    assert new != old
    stale = writer.publish(uuid4(), source, utc_now(), output)
    assert stale.outcome == "stale" and stale.error_code == "stale_raw_version"
    state = writer.status(parser, chunker)[0]
    assert state["state"] == "pending" and state["raw_version_id"] == new
    assert state["passage_set_id"] is None
    assert process_pilot(writer, artifacts).documents[0].outcome == "created"
    assert writer.status(parser, chunker)[0]["state"] == "ready"
    assert counts(store.engine)["fomc_passage_sets"] == 2


def test_concurrent_identical_work_creates_one_set(
    store: FomcStore,  # noqa: F811
    tmp_path: Path,
) -> None:
    writer, artifacts = FomcProcessingStore(store.engine), ArtifactStore(tmp_path)
    collect_one(store, artifacts)
    source = writer.inputs()[SPEC.document_id]
    output = process_original(source, artifacts, DEFAULT_PARSER_CONFIG, DEFAULT_CHUNKER_CONFIG)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(writer.publish, uuid4(), source, utc_now(), output) for _ in range(2)]
        results = [task.result(timeout=15) for task in tasks]
    assert {item.outcome for item in results} == {"created", "reused"}
    assert len({item.passage_set_id for item in results}) == 1
    assert counts(store.engine)["fomc_passage_sets"] == 1
    assert counts(store.engine)["fomc_processing_attempts"] == 2


def test_write_failure_rolls_back_every_output_and_status_reports_failure(
    store: FomcStore,  # noqa: F811
    tmp_path: Path,
) -> None:
    writer, artifacts = FomcProcessingStore(store.engine), ArtifactStore(tmp_path)
    collect_one(store, artifacts)

    def reject_spans(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        many: bool,
    ) -> None:
        if statement.startswith("INSERT INTO fomc_passage_spans"):
            raise OperationalError(statement, {}, RuntimeError("synthetic transaction failure"))

    sa.event.listen(store.engine, "before_cursor_execute", reject_spans)
    try:
        result = process_pilot(writer, artifacts).documents[0]
    finally:
        sa.event.remove(store.engine, "before_cursor_execute", reject_spans)
    assert result.outcome == "failed" and result.error_code == "database_error"
    assert result.audit_recorded
    assert counts(store.engine) == dict.fromkeys(PROCESSING_TABLE_NAMES, 0) | {
        "fomc_processing_attempts": 1,
    }
    assert writer.status(DEFAULT_PARSER_CONFIG, DEFAULT_CHUNKER_CONFIG)[0]["state"] == "failed"
    assert process_pilot(writer, artifacts).documents[0].outcome == "created"


def test_replay_detects_conflict_without_rewriting_rows(
    store: FomcStore,  # noqa: F811
    admin_engine: Engine,
    tmp_path: Path,
) -> None:
    writer, artifacts = FomcProcessingStore(store.engine), ArtifactStore(tmp_path)
    collect_one(store, artifacts)
    assert process_pilot(writer, artifacts).documents[0].outcome == "created"
    with admin_engine.begin() as conn:
        conn.execute(
            fomc_parsed_blocks.update()
            .where(fomc_parsed_blocks.c.block_ordinal == 0)
            .values(body="Simulated inconsistency")
        )
    result = process_pilot(writer, artifacts).documents[0]
    assert result.outcome == "failed" and result.error_code == "output_conflict"
    with admin_engine.connect() as conn:
        assert (
            conn.execute(
                sa.select(fomc_parsed_blocks.c.body).where(fomc_parsed_blocks.c.block_ordinal == 0)
            ).scalar_one()
            == "Simulated inconsistency"
        )


def test_span_and_audit_foreign_keys_keep_generation_lineage(
    store: FomcStore,  # noqa: F811
    tmp_path: Path,
) -> None:
    writer, artifacts = FomcProcessingStore(store.engine), ArtifactStore(tmp_path)
    collect_one(store, artifacts)
    first = process_pilot(writer, artifacts).documents[0]
    collect_one(store, artifacts, day=1, suffix=" New language.")
    second = process_pilot(writer, artifacts).documents[0]
    with store.engine.begin() as conn:
        a, b = [
            dict(
                conn.execute(
                    sa.select(fomc_processing_attempts).where(
                        fomc_processing_attempts.c.passage_set_id == result.passage_set_id
                    )
                )
                .mappings()
                .one()
            )
            for result in (first, second)
        ]
        passage = conn.execute(
            sa.select(fomc_passages.c.passage_id).where(
                fomc_passages.c.passage_set_id == first.passage_set_id
            )
        ).scalar_one()
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                fomc_passage_spans.insert().values(
                    passage_id=passage,
                    span_ordinal=99,
                    generation_id=b["generation_id"],
                    block_ordinal=0,
                    start_offset=0,
                    end_offset=1,
                )
            )
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(
                fomc_processing_attempts.insert(),
                a
                | dict(
                    attempt_id=uuid4(),
                    run_id=uuid4(),
                    outcome="reused",
                    passage_set_id=b["passage_set_id"],
                ),
            )
        assert conn.scalar(sa.select(sa.func.count()).select_from(fomc_passage_sets)) == 2
