"""CLI commands execute real provider parsing and writer transactions without network."""

import json

import httpx
import pytest
import sqlalchemy as sa

from market_intelligence import cli
from market_intelligence.config import DatabaseRole, DatabaseSettings
from market_intelligence.db.macro_store import MacroStore
from market_intelligence.db.macro_tables import macro_ingestion_runs as runs
from market_intelligence.db.macro_tables import macro_observed_versions as versions
from market_intelligence.macro import cli as macro_cli
from market_intelligence.macro.bls import BLS_URL
from market_intelligence.macro.fed import FED_URL
from tests.integration.test_macro_ingestion import Clock, bls_body
from tests.unit.test_fed import archive, document

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("command", ["ingest-bls", "ingest-fed"])
def test_cli_ingests_with_writer_credentials_then_reports_zero_new_writes_on_resume(
    macro_store: MacroStore,
    database_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    clock, calls = Clock(), 0
    original_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert str(request.url) == (BLS_URL if command == "ingest-bls" else FED_URL)
        return (
            httpx.Response(200, json=bls_body(request))
            if command == "ingest-bls"
            else httpx.Response(200, content=archive(document()))
        )

    def engine(settings: DatabaseSettings, role: DatabaseRole) -> sa.Engine:
        assert settings is database_settings and role == DatabaseRole.INGEST
        return macro_store.engine

    monkeypatch.setattr(cli, "DatabaseSettings", lambda: database_settings)
    monkeypatch.setattr(cli, "utc_now", clock.now)
    monkeypatch.setattr(macro_cli, "utc_now", clock.now)
    monkeypatch.setattr(macro_cli, "create_db_engine", engine)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    year = "2020" if command == "ingest-bls" else "2024"
    arguments = [command, "--start", f"{year}-01-01", "--end", f"{year}-03-01"]
    assert cli.main(arguments) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    expected = 4 if command == "ingest-bls" else 2
    assert events[-1]["inserted"] == expected and events[-1]["requests_used"] == 1
    assert events[1]["absent_from_read"] == 0
    assert events[-1]["publication_calendar_completeness"] == "not_established"
    assert cli.main([*arguments, "--resume"]) == 0
    skipped = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert skipped[1]["event"] == "macro_window_skipped"
    assert skipped[1]["inserted"] == skipped[-1]["inserted"] == 0
    assert skipped[-1]["skipped_windows"] == 1 and skipped[-1]["requests_used"] == 0
    assert calls == 1
    with macro_store.engine.connect() as conn:
        assert (
            conn.execute(sa.select(sa.func.count()).select_from(versions)).scalar_one() == expected
        )
        assert conn.execute(sa.select(sa.func.count()).select_from(runs)).scalar_one() == 1


@pytest.mark.parametrize(
    "failure,code,exit_code",
    [
        ("http", "http_error", 1),
        ("interrupt", "interrupted", 130),
        ("quota", "retry_exhausted", 1),
    ],
)
def test_cli_failure_exit_codes_audit_and_request_budget_are_sanitized(
    macro_store: MacroStore,
    database_settings: DatabaseSettings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    code: str,
    exit_code: int,
) -> None:
    original_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "interrupt":
            raise KeyboardInterrupt
        return httpx.Response(
            503 if failure == "quota" else 400, text="Private credentials and payload"
        )

    clock = Clock()
    monkeypatch.setattr(cli, "DatabaseSettings", lambda: database_settings)
    monkeypatch.setattr(cli, "utc_now", clock.now)
    monkeypatch.setattr(macro_cli, "utc_now", clock.now)
    monkeypatch.setattr(macro_cli, "create_db_engine", lambda *_: macro_store.engine)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler),
            **kwargs,
        ),
    )
    assert (
        cli.main(
            ["ingest-bls", "--start", "2020-01-01", "--end", "2020-03-01", "--max-requests", "1"]
        )
        == exit_code
    )
    output = capsys.readouterr()
    error = json.loads(output.err)
    assert error["code"] == code and error["audit_recorded"] is True
    assert error["requests_used"] == error["request_limit"] == 1
    assert "Private" not in output.out + output.err
    with macro_store.engine.connect() as conn:
        assert conn.execute(sa.select(runs.c.error_code)).scalar_one() == code
        assert conn.execute(sa.select(sa.func.count()).select_from(versions)).scalar_one() == 0
