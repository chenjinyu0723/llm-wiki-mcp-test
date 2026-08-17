"""Local-only configuration for the MCP validation console."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    host: str = "127.0.0.1"
    port: int = Field(default=8044, ge=1, le=65535)
    public_mcp_url: str = "http://127.0.0.1:8030/mcp"
    admin_mcp_url: str = "http://127.0.0.1:8031/mcp"
    data_dir: Path = Path("data")
    upload_dir: Path = Path("data/uploads")
    max_upload_files: int = Field(default=20, ge=1, le=50)
    max_upload_bytes: int = Field(default=100 * 1024 * 1024, ge=1_024_000)
    chat_context_chars: int = Field(default=60_000, ge=8_000, le=180_000)
    chat_history_messages: int = Field(default=12, ge=2, le=30)
    chat_timeout_seconds: float = Field(default=600, ge=10, le=3600)
    chat_max_retries: int = Field(default=2, ge=0, le=4)

    model_config = SettingsConfigDict(
        env_file=PROJECT_DIR / ".env",
        env_prefix="VALIDATOR_",
        extra="ignore",
    )

    def model_post_init(self, __context: object) -> None:
        if not self.data_dir.is_absolute():
            self.data_dir = PROJECT_DIR / self.data_dir
        if not self.upload_dir.is_absolute():
            self.upload_dir = PROJECT_DIR / self.upload_dir

    @property
    def database_path(self) -> Path:
        return self.data_dir / "validator.db"


settings = Settings()
