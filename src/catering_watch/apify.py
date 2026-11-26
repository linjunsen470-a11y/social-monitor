from __future__ import annotations
import httpx
from .config import settings

class ApifyError(RuntimeError):
    pass

class ApifyClient:
    """Direct Apify REST API client. Every Actor run must have an explicit USD cap."""
    base = "https://api.apify.com/v2"

    def __init__(self, token: str | None = None):
        self.token = token if token is not None else settings.apify_token
        if not self.token or "<YOUR_TOKEN>" in self.token:
            raise ApifyError("APIFY_TOKEN 未配置；请在 .env 中填写 Apify API token")
        self.client = httpx.Client(
            timeout=settings.http_timeout_seconds,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
        )

    def check_auth(self) -> dict:
        response = self.client.get(f"{self.base}/users/me")
        if response.is_error:
            raise ApifyError(f"Apify token check failed: HTTP {response.status_code}")
        data = response.json().get("data", {})
        plan = data.get("plan") or {}
        return {"username": data.get("username"), "id": data.get("id"), "plan": plan.get("id")}

    def run(self, actor: str, payload: dict, max_cost_usd: float, wait_seconds: int = 45):
        if max_cost_usd <= 0:
            raise ApifyError("max_cost_usd must be > 0")
        actor_ref = actor.replace("/", "~")
        response = self.client.post(
            f"{self.base}/acts/{actor_ref}/runs",
            params={"waitForFinish": min(max(wait_seconds, 0), 300),
                    "maxTotalChargeUsd": f"{max_cost_usd:.4f}"},
            json=payload,
        )
        if response.is_error:
            raise ApifyError(f"Actor start failed: HTTP {response.status_code}: {response.text[:400]}")
        run = response.json().get("data", {})
        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            raise ApifyError(f"Actor returned no dataset ID (status={run.get('status')})")
        if run.get("status") not in {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"}:
            raise ApifyError(f"Actor run did not succeed: {run.get('status')} {run.get('statusMessage','')}")
        items_response = self.client.get(
            f"{self.base}/datasets/{dataset_id}/items",
            params={"clean": "true", "limit": 100},
        )
        if items_response.is_error:
            raise ApifyError(f"Dataset fetch failed: HTTP {items_response.status_code}")
        items = items_response.json()
        if not isinstance(items, list):
            raise ApifyError("Unexpected Apify Dataset response; expected a list")
        return run, items
