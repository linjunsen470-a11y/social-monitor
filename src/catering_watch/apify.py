from __future__ import annotations
import time
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

    def start(self, actor, payload, cap, build, wait=0):
        response = self.client.post(
            f"{self.base}/acts/{actor.replace('/', '~')}/runs",
            params={"waitForFinish": wait, "maxTotalChargeUsd": f"{cap:.4f}", "build": build},
            json=payload,
        )
        if response.is_error:
            raise ApifyError(f"Actor start: HTTP {response.status_code}")
        return response.json()["data"]

    def status(self, provider):
        response = self.client.get(f"{self.base}/actor-runs/{provider}")
        response.raise_for_status()
        return response.json()["data"]

    def page(self, dataset, offset, limit):
        response = self.client.get(
            f"{self.base}/datasets/{dataset}/items", params={"offset": offset, "limit": limit}
        )
        if response.is_error:
            raise ApifyError(f"Dataset: HTTP {response.status_code}")
        rows = response.json()
        if not isinstance(rows, list):
            raise ApifyError("Expected dataset items")
        return rows

    def run(self, actor, payload, max_cost_usd):
        """Standalone transport convenience; CLI adapters use the durable client."""
        run = self.start(actor, payload, max_cost_usd, "latest", 45)
        deadline = time.monotonic() + settings.http_timeout_seconds
        while (
            run["status"] in {"READY", "RUNNING", "TIMING-OUT", "ABORTING"}
            and time.monotonic() < deadline
        ):
            time.sleep(1)
            run = self.status(run["id"])
        if run["status"] not in {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"}:
            raise ApifyError(f"Run {run['id']} status {run['status']}")
        rows = []
        while True:
            page = self.page(run["defaultDatasetId"], len(rows), 100)
            rows.extend(page)
            if len(page) < 100:
                return run, rows
