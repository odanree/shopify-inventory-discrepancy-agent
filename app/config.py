from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Shopify
    shopify_shop_domain: str = "example.myshopify.com"
    shopify_access_token: str = ""
    shopify_api_version: str = "2024-01"
    shopify_webhook_secret: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    agent_model: str = "claude-sonnet-4-6"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # PostgreSQL
    database_url: str = (
        "postgresql+asyncpg://agent_user:secret@localhost:5432/inventory_discrepancy_agent"
    )

    # Google Sheets
    google_service_account_json: str = "/run/secrets/gcp-sa.json"
    audit_spreadsheet_id: str = ""

    # Slack
    slack_webhook_url: str = ""
    slack_alerts_channel: str = "#inventory-alerts"
    slack_signing_secret: str = ""  # for verifying interactive action callbacks

    # LangFuse tracing
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    langfuse_enabled: bool = True

    # Discrepancy detection
    discrepancy_threshold_pct: float = 5.0

    # Scheduler
    scheduler_enabled: bool = True
    scheduler_interval_minutes: int = 60

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    port: int = 8000
    approval_expiry_hours: int = 24


@lru_cache
def get_settings() -> Settings:
    return Settings()
