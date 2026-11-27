import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def credential(value):
    value = (value or "").strip()
    return "" if "<" in value else value


def configured_token():
    return credential(os.getenv("APIFY_TOKEN")) or credential(os.getenv("APIFY_API_KEY"))


def env_value(name, default):
    return os.getenv(name, "").strip() or default


runtime_token = configured_token()
local_env = Path.cwd() / ".env"
load_dotenv(local_env)


@dataclass(frozen=True)
class Settings:
    database_url: str = credential(os.getenv("DATABASE_URL"))
    apify_token: str = runtime_token or configured_token()
    project_key: str = os.getenv("PROJECT_KEY", "guangzhou_catering_competitors")
    max_cost_usd: float = float(env_value("MAX_COST_USD", "1.00"))
    cache_ttl_hours: int = int(env_value("CACHE_TTL_HOURS", "24"))
    max_items_per_search: int = int(env_value("MAX_ITEMS_PER_SEARCH", "5"))
    http_timeout_seconds: int = int(env_value("HTTP_TIMEOUT_SECONDS", "90"))


settings = Settings()
