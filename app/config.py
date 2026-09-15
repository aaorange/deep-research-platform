from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "deep-research-platform"
    app_version: str = "0.1.0"
    debug: bool = True

    database_url: str = "postgresql+asyncpg://research:research@localhost:5432/research"
    redis_url: str = "redis://localhost:6379/1"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    llm_model_chat: str = "deepseek-chat"
    llm_model_reasoner: str = "deepseek-reasoner"

    bocha_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
