from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    database_url: str  # postgresql+asyncpg://user:pass@host:5432/db
    bot_token: str = ""  # checked in app.main, so migrations/tests don't need it
    initial_manager_telegram_id: int | None = None
    company_name: str = "Hydroclass"  # used only when the company row is first created
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
