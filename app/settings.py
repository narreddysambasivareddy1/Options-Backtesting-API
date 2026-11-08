from pydantic_settings import BaseSettings, SettingsConfigDict

from app.paths import ROOT


class Settings(BaseSettings):
    app_env: str = "development"
    upstox_client_id: str = ""
    upstox_client_secret: str = ""
    upstox_redirect_uri: str = "http://127.0.0.1:8000/auth/callback"
    upstox_api_base: str = "https://api.upstox.com"
    timezone: str = "Asia/Kolkata"

    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
