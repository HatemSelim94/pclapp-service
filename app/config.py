"""Settings, read once from the environment (or a .env file)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PCLAPP_", env_file=".env", extra="ignore")

    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+psycopg://postgres:postgres@localhost:5432/pclapp"),
        description="SQLAlchemy URL; must use the psycopg (v3) driver.",
    )
    data_dir: Path = Field(default=Path("/data"), description="Root for uploaded cloud files.")
    admin_key: SecretStr = Field(
        default=SecretStr("change-me"),
        description="Plain admin key. Only this key may create users and issue API keys.",
    )
    max_upload_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    allowed_formats: frozenset[str] = frozenset({"pcd", "txt"})
    page_size_default: int = Field(default=50, ge=1, le=500)
    page_size_max: int = Field(default=200, ge=1, le=1000)

    # worker
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    worker_max_attempts: int = Field(default=3, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
