"""
app/core/config.py
──────────────────
Central configuration loaded from .env file.
All settings are validated by Pydantic at startup — the app will refuse
to start if a required variable is missing or has the wrong type.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All application settings.
    Values are read from environment variables or .env file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Application ────────────────────────────────────────────────────────────
    app_name: str = "Generator Monitoring Platform"
    app_version: str = "1.0.0"
    debug: bool = False

    # ── Security ───────────────────────────────────────────────────────────────
    secret_key: str
    access_token_expire_minutes: int = 480

    # ── MQTT ───────────────────────────────────────────────────────────────────
    mqtt_broker_host: str = "broker.hivemq.com" #"localhost"
    mqtt_broker_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_client_id: str = "generator_platform_backend"
    mqtt_keepalive: int = 60
    mqtt_data_topic: str = "generator/+/data"
    mqtt_command_topic_template: str = "generator/{device_id}/command"

    # ── Database ───────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./generator_platform.db"

    # ── Redis ──────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    # Channel prefix — full channel = "device:gen_01"
    redis_channel_prefix: str = "device"

    # ── Server ─────────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 4           # Number of Uvicorn worker processes

    # ── CORS ───────────────────────────────────────────────────────────────────
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse comma-separated CORS origins into a list."""
        return [origin.strip() for origin in self.cors_origins.split(",")]

    def get_command_topic(self, device_id: str) -> str:
        """Return the MQTT publish topic for a given device."""
        return self.mqtt_command_topic_template.format(device_id=device_id)

    def get_redis_channel(self, device_id: str) -> str:
        """Redis pub/sub channel name for a device."""
        return f"{self.redis_channel_prefix}:{device_id}"


@lru_cache
def get_settings() -> Settings:
    """
    Return a cached singleton of Settings.
    Using lru_cache ensures .env is read only once at startup.
    """
    return Settings()
