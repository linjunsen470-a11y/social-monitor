import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def configured_token():
    return next(
        (
            value
            for name in ("APIFY_TOKEN", "APIFY_API_KEY")
            if (value := os.getenv(name)) and "<" not in value
        ),
        "",
    )


runtime_token = configured_token()
local_env = Path.cwd() / ".env"
load_dotenv(local_env if local_env.is_file() else None)


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "")
    apify_token: str = runtime_token or configured_token()
    project_key: str = os.getenv("PROJECT_KEY", "guangzhou_catering_competitors")
    max_cost_usd: float = float(os.getenv("MAX_COST_USD", "1.00"))
    cache_ttl_hours: int = int(os.getenv("CACHE_TTL_HOURS", "24"))
    max_items_per_search: int = int(os.getenv("MAX_ITEMS_PER_SEARCH", "5"))
    http_timeout_seconds: int = int(os.getenv("HTTP_TIMEOUT_SECONDS", "90"))


settings = Settings()
