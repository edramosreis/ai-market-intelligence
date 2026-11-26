from pathlib import Path
from unittest.mock import Mock

import pytest
from alembic.config import Config
from pydantic import SecretStr

from market_intelligence import cli
from market_intelligence.config import DatabaseSettings
from market_intelligence.db import fomc_vector_runtime


@pytest.mark.parametrize(
    ("host", "port", "database"),
    [
        ("db", 5432, "market_intelligence"),
        ("db-test", 5432, "market_intelligence_test"),
        ("db", 55432, "market_intelligence_m3"),
        ("127.0.0.1", 55433, "market_intelligence_m3"),
    ],
)
def test_vector_commands_refuse_other_targets_before_connecting(
    host: str, port: int, database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = DatabaseSettings(db=database, host=host, port=port, _env_file=None)  # type: ignore[call-arg]
    connect = Mock()
    monkeypatch.setattr(fomc_vector_runtime, "create_db_engine", connect)
    with pytest.raises(ValueError, match="isolated M3 database"):
        fomc_vector_runtime.initialize_vector_runtime(settings, Config())
    with pytest.raises(ValueError, match="isolated M3 database"):
        fomc_vector_runtime.check_vector_runtime(settings)
    connect.assert_not_called()


def test_vector_commands_refuse_other_roles_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = DatabaseSettings(  # type: ignore[call-arg]
        db="market_intelligence_m3",
        host="db",
        port=5432,
        admin_user="market_admin",
        admin_password=SecretStr("synthetic"),
        ingest_user="market_ingest",
        ingest_password=SecretStr("synthetic"),
        read_user="market_reader",
        read_password=SecretStr("synthetic"),
        _env_file=None,
    )
    connect = Mock()
    monkeypatch.setattr(fomc_vector_runtime, "create_db_engine", connect)
    with pytest.raises(ValueError, match="existing M3 roles"):
        fomc_vector_runtime.initialize_vector_runtime(settings, Config())
    with pytest.raises(ValueError, match="M3 reader role"):
        fomc_vector_runtime.check_vector_runtime(settings)
    connect.assert_not_called()


def test_cli_selects_vector_only_explicitly_and_refuses_arbitrary_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "alembic.ini").touch()
    monkeypatch.setenv("POSTGRES_DB", "market_intelligence_m3")
    stock = Mock()
    vectors = Mock()
    check = Mock()
    monkeypatch.setattr(cli, "initialize_database", stock)
    monkeypatch.setattr(cli, "initialize_vector_runtime", vectors)
    monkeypatch.setattr(cli, "check_vector_runtime", check)
    assert cli.main(["init-db"]) == 0
    stock.assert_called_once()
    vectors.assert_not_called()
    assert cli.main(["init-db", "--fomc-vectors"]) == 0
    assert cli.main(["check-db", "--fomc-vectors"]) == 0
    vectors.assert_called_once()
    check.assert_called_once()
    for bad in (
        ["init-db", "--target-revision", "head"],
        ["check-db", "--fomc-vectors", "--role", "admin"],
    ):
        with pytest.raises(SystemExit) as result:
            cli.main(bad)
        assert result.value.code == 2
