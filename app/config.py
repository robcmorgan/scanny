from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./scanny.db"
    drive_incoming_folder_id: str = ""
    drive_filed_root_folder_id: str = ""
    google_client_secret_file: str = "../secrets/client_secret.json"
    google_token_file: str = "../data/token.json"
    poll_interval_seconds: int = 30
    app_base_url: str = "http://localhost:9841"

    class Config:
        env_file = ".env"


settings = Settings()
