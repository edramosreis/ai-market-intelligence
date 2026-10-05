import os
import secrets
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from market_intelligence.config import DatabaseRole, DatabaseSettings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for key in tuple(os.environ):
        if key.startswith("POSTGRES_"):
            monkeypatch.delenv(key)


def test_reader_can_connect_without_admin_or_ingest_credentials() -> None:
    settings = DatabaseSettings(
        db="market_intelligence",
        read_user="reader",
        read_password=SecretStr(secrets.token_urlsafe(24)),
        admin_user=None,
        admin_password=None,
        ingest_user=None,
        ingest_password=None,
    )
    assert settings.url(DatabaseRole.READ).username == "reader"
    with pytest.raises(ValueError, match="Missing POSTGRES_ADMIN"):
        settings.url(DatabaseRole.ADMIN)


def test_password_special_characters_are_preserved_and_masked() -> None:
    password = secrets.token_urlsafe(24) + "@:/%?#"
    settings = DatabaseSettings(
        db="market_intelligence",
        read_user="reader",
        read_password=SecretStr(password),
    )
    url = settings.url(DatabaseRole.READ)
    assert url.password == password
    assert password not in str(url)
    assert password not in repr(settings)


def test_placeholder_password_is_rejected() -> None:
    settings = DatabaseSettings(
        db="market_intelligence",
        read_user="reader",
        read_password=SecretStr("REPLACE_WITH_LOCAL_READ_PASSWORD"),
    )
    with pytest.raises(ValueError, match="placeholders"):
        settings.credentials(DatabaseRole.READ)


def test_roles_must_be_distinct() -> None:
    settings = DatabaseSettings(
        db="market_intelligence",
        admin_user="same_role",
        admin_password=SecretStr(secrets.token_urlsafe(24)),
        ingest_user="same_role",
        ingest_password=SecretStr(secrets.token_urlsafe(24)),
        read_user="reader",
        read_password=SecretStr(secrets.token_urlsafe(24)),
    )
    with pytest.raises(ValueError, match="distinct"):
        settings.validate_role_names()


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_port_is_rejected(port: int) -> None:
    with pytest.raises(ValidationError):
        DatabaseSettings(db="market_intelligence", port=port)


def test_invalid_identifier_is_rejected_without_echoing_input() -> None:
    unsafe_name = "db; unexpected input"
    with pytest.raises(ValidationError) as error:
        DatabaseSettings(db=unsafe_name)
    assert unsafe_name not in str(error.value)
