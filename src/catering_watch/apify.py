from __future__ import annotations

import re

import httpx

from .config import credential, settings


class ApifyError(RuntimeError):
    pass


def safe_error(exc):
    return str(exc) if isinstance(exc, ApifyError) else type(exc).__name__


class StartRejected(ApifyError):
    """The provider rejected the request before creating a run."""


class ApifyClient:
    """Direct Apify REST API client. Every Actor run must have an explicit USD cap."""

    base = "https://api.apify.com/v2"

    def __init__(self, token: str | None = None):
        self.token = credential(token) if token is not None else settings.apify_token
        if not self.token:
            raise ApifyError("APIFY_TOKEN 未配置；请在 .env 中填写 Apify API token")
        self.client = httpx.Client(
            timeout=settings.http_timeout_seconds,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.client.close()

    def check_auth(self) -> dict:
        response = self.client.get(f"{self.base}/users/me")
        if response.is_error:
            raise ApifyError(f"Apify token check failed: HTTP {response.status_code}")
        data = response.json().get("data", {})
        plan = data.get("plan") or {}
        return {"username": data.get("username"), "id": data.get("id"), "plan": plan.get("id")}

    def start(self, actor, payload, cap, build):
        response = self.client.post(
            f"{self.base}/acts/{actor.replace('/', '~')}/runs",
            params={"waitForFinish": 0, "maxTotalChargeUsd": f"{cap:.4f}", "build": build},
            json=payload,
        )
        if response.is_error:
            error = f"Actor start: HTTP {response.status_code}"
            # Error messages can echo signed input URLs. The error type is sufficient for diagnosis.
            try:
                code = response.json()["error"]["type"]
                if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", code):
                    error += f" ({code})"
            except (ValueError, TypeError, KeyError):
                pass
            if response.status_code in {400, 401, 402, 403, 404, 405, 413, 415, 422, 429}:
                raise StartRejected(error)
            raise ApifyError(error)
        return response.json()["data"]

    def status(self, provider):
        response = self.client.get(f"{self.base}/actor-runs/{provider}")
        if not response.is_success:
            raise ApifyError(f"Actor status: HTTP {response.status_code}")
        return response.json()["data"]

    def page(self, dataset, offset, limit):
        response = self.client.get(
            f"{self.base}/datasets/{dataset}/items",
            # Keep empty rows: offsets and the end-of-dataset check count physical items.
            params={"offset": offset, "limit": limit, "skipHidden": "true"},
        )
        if response.is_error:
            raise ApifyError(f"Dataset: HTTP {response.status_code}")
        rows = response.json()
        if not isinstance(rows, list):
            raise ApifyError("Expected dataset items")
        return rows
