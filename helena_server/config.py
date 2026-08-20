"""Server settings, read from the environment (and a .env file if present)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_db_path() -> str:
    return str(Path.home() / ".helena" / "helena.db")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HELENA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Upstream Ollama
    ollama_host: str = "http://127.0.0.1:11434"

    # This server
    host: str = "127.0.0.1"
    port: int = 8080

    # Model defaults. Callers may override any of these per request.
    default_model: str = "qwen2.5:7b-instruct"
    vision_model: str = "llava"
    embed_model: str = "nomic-embed-text"

    # Ollama allocates KV cache proportional to num_ctx regardless of how
    # short the actual conversation is, so this is the biggest single lever
    # on memory use for a local setup. 8192 leaves room for an agent loop's
    # tool results; 4096 is friendlier to small machines.
    num_ctx: int = 8192
    temperature: float = 0.7

    # Generations run concurrently against one Ollama process contend for the
    # same GPU/CPU; more than a couple in flight usually makes everything
    # slower rather than faster.
    max_concurrency: int = 2
    request_timeout: float = 600.0

    # Optional bearer token. Empty means no auth, which is fine when bound to
    # localhost and the intended default for a personal machine.
    api_token: str = ""

    db_path: str = ""
    cors_origins: str = "*"

    @property
    def resolved_db_path(self) -> Path:
        raw = self.db_path or _default_db_path()
        return Path(os.path.expanduser(raw))

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
