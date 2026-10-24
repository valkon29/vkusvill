"""Настройки сервиса: читаются из .env или переменных окружения."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_api_key: str = ""
    llm_base_url: str = "https://ai.api.cloud.yandex.net/v1"
    # YandexGPT через OpenAI-совместимый API: gpt://<folder_id>/yandexgpt/latest
    model_name: str = "gpt://b1g5ho4goimftu37qumf/yandexgpt/latest"

    mcp_url: str = "https://mcp.vkusvill.ru/mcp"

    max_tool_iterations: int = 6
    tool_result_max_chars: int = 6000


settings = Settings()
