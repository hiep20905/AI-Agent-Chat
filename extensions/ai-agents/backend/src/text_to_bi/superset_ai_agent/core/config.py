from datetime import timedelta, timezone

from pydantic_settings import BaseSettings, SettingsConfigDict

# "Hôm nay" and "tuần này" are read in Vietnam's time, whatever the server's clock zone.
BUSINESS_TIMEZONE = timezone(timedelta(hours=7))


class Settings(BaseSettings):
    # Model every agent step uses.
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_MODEL: str = "openai/gpt-6-luna"

    # Superset as seen from inside its own container, where the agent reads the Semantic Layer's endpoints.
    TEXT_TO_BI_SUPERSET_INTERNAL_URL: str = "http://127.0.0.1:8088"

    model_config = SettingsConfigDict(case_sensitive=True, extra="ignore")


settings = Settings()
