from dataclasses import dataclass
import os
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "")
    apify_token: str = os.getenv("APIFY_TOKEN", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    project_key: str = os.getenv("PROJECT_KEY", "guangzhou_catering_competitors")
    max_cost_usd: float = float(os.getenv("MAX_COST_USD", "1.00"))
    cache_ttl_hours: int = int(os.getenv("CACHE_TTL_HOURS", "24"))
    max_items_per_search: int = int(os.getenv("MAX_ITEMS_PER_SEARCH", "5"))
    http_timeout_seconds: int = int(os.getenv("HTTP_TIMEOUT_SECONDS", "90"))

settings = Settings()
