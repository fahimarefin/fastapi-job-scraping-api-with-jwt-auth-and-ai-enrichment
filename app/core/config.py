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


@lru_cache
def get_settings() -> Settings:
    return Settings()