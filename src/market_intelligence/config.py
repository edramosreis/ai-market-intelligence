"""Process configuration without secret-bearing connection-string logging."""

from enum import StrEnum
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
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
    macro_max_window_months: int = Field(default=1200, ge=1, le=1200)
    stale_after_seconds: int = Field(default=900, ge=1, le=86400)
    funding_stale_after_seconds: int = Field(default=7200, ge=3600, le=604800)
    open_interest_stale_after_seconds: int = Field(default=3600, ge=1, le=604800)


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    enabled: bool = False
    api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    model: str | None = Field(default=None, max_length=200, validation_alias="OPENAI_MODEL")
    deadline_seconds: float = Field(default=60, gt=0, le=60, allow_inf_nan=False)
    max_model_requests: int = Field(default=4, ge=1, le=4)
    max_tool_calls: int = Field(default=3, ge=1, le=3)
    max_question_chars: int = Field(default=4000, ge=1, le=4000)
    max_output_tokens: int = Field(default=2048, ge=64, le=4096)
    max_answer_chars: int = Field(default=8000, ge=1, le=16000)
    max_context_bytes: int = Field(default=128000, ge=1024, le=256000)
    max_tool_output_bytes: int = Field(default=32000, ge=1024, le=64000)
    max_response_bytes: int = Field(default=64000, ge=1024, le=128000)

    @field_validator("api_key", "model", mode="before")
    @classmethod
    def empty_optional_value(cls, value: object) -> object:
        return (value.strip() or None) if isinstance(value, str) else value

    @property
    def configured(self) -> bool:
        return bool(
            self.enabled
            and self.api_key
            and self.api_key.get_secret_value()
            and not self.api_key.get_secret_value().startswith("REPLACE_")
            and self.model
            and not self.model.startswith("REPLACE_")
        )


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
