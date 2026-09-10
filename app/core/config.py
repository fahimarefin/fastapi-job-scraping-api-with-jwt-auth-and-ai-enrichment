#app/core/config.py
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Job Scraper API"
    app_version: str = "1.0.0"
    debug: bool = False

    request_timeout: int = 15
    max_retries: int = 3
    default_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    http_proxy: str | None = None
    career_pages_config_path: str = "app/core/career_pages.json"
    min_request_interval: float = 0.2

    # Candidate scraping — delay between LinkedIn profile requests (seconds)
    candidate_scrape_delay: float = 2.0

    # Fresh job filtering & decision maker enrichment
    default_hours_old: int = 48
    enrich_decision_makers: bool = True
    max_decision_makers_per_company: int = 2
    hunter_api_key: str | None = None

    # MongoDB Database settings
    mongodb_url: str = (
        "mongodb://rebekhoffman:xK9mP2vQ8nR5tL1wZ7yB4cF6hJ3sD0aE@127.0.0.1:27017/myappdb?authSource=admin"
    )
    mongodb_db_name: str = "myappdb"

    # JWT Authentication settings
    jwt_secret_key: str = "xK9mP2vQ8nR5tL1wZ7yB4cF6hJ3sD0aE"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 1440  # 24 hours



@lru_cache
def get_settings() -> Settings:
    return Settings()