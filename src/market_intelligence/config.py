"""Process configuration without secret-bearing connection-string logging."""

from enum import StrEnum
from typing import Annotated

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")]


class DatabaseRole(StrEnum):
    ADMIN = "admin"
    INGEST = "ingest"
    READ = "read"


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="API_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    max_window_days: int = Field(default=3653, ge=1, le=36525)
    stale_after_seconds: int = Field(default=900, ge=1, le=86400)


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POSTGRES_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    db: Identifier
    host: str = "127.0.0.1"
    port: int = Field(default=55432, ge=1, le=65535)
    admin_user: Identifier | None = None
    admin_password: SecretStr | None = None
    ingest_user: Identifier | None = None
    ingest_password: SecretStr | None = None
    read_user: Identifier | None = None
    read_password: SecretStr | None = None

    def credentials(self, role: DatabaseRole) -> tuple[str, SecretStr]:
        user: str | None = getattr(self, f"{role.value}_user")
        password: SecretStr | None = getattr(self, f"{role.value}_password")
        if user is None or password is None or not password.get_secret_value():
            raise ValueError(f"Missing POSTGRES_{role.value.upper()}_USER/PASSWORD configuration")
        if user.startswith("REPLACE_") or password.get_secret_value().startswith("REPLACE_"):
            raise ValueError("Replace configuration placeholders in a local .env")
        return user, password

    def url(self, role: DatabaseRole) -> URL:
        user, password = self.credentials(role)
        return URL.create(
            "postgresql+psycopg",
            username=user,
            password=password.get_secret_value(),
            host=self.host,
            port=self.port,
            database=self.db,
        )

    def validate_role_names(self) -> tuple[str, str, str]:
        names = tuple(self.credentials(role)[0] for role in DatabaseRole)
        if len(set(names)) != 3:
            raise ValueError("Database admin, ingestion, and reader roles must be distinct")
        return names[0], names[1], names[2]
