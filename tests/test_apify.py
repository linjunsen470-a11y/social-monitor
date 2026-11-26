import pytest
import httpx

from catering_watch.apify import ApifyClient, ApifyError, StartRejected

class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.is_error = status_code >= 400
        self.text = "mock error"
    def json(self):
        return self._data

class FakeHttpClient:
    def __init__(self, *args, **kwargs):
        self.calls = []
    def post(self, url, params=None, json=None):
        self.calls.append(("POST", url, params, json))
        return FakeResponse({"data":{"id":"run1","status":"SUCCEEDED","defaultDatasetId":"dataset1",
                                     "usageTotalUsd":0.012}})
    def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        return FakeResponse([{"note_id":"abc"}])

def test_apify_run_uses_token_and_hard_cost_cap(monkeypatch):
    import catering_watch.apify as module
    clients=[]
    def factory(*args, **kwargs):
        c=FakeHttpClient()
        clients.append(c)
        return c
    monkeypatch.setattr(module.httpx, "Client", factory)
    client=ApifyClient(token="apify_api_testtoken")
    run, rows=client.run("vendor/actor", {"max_items":1}, 0.10)
    assert run["id"] == "run1"
    assert rows == [{"note_id":"abc"}]
    post=clients[0].calls[0]
    assert post[2]["maxTotalChargeUsd"] == "0.1000"
    assert "vendor~actor" in post[1]
    assert clients[0].calls[1][2] == {"offset": 0, "limit": 100, "skipHidden": "true"}


@pytest.mark.parametrize(
    "status,rejected",
    [(400, True), (401, True), (403, True), (422, True), (408, False), (500, False)],
)
def test_start_rejection_keeps_safe_provider_diagnostics(status, rejected):
    def respond(request):
        return httpx.Response(
            status, json={"error": {"type": "invalid-input", "message": "private-input"}}
        )

    client = ApifyClient("test")
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        client.client = http
        with pytest.raises(ApifyError, match="invalid-input") as error:
            client.start("vendor/actor", {}, 0.1, "latest")
    assert isinstance(error.value, StartRejected) is rejected
    assert "private-input" not in str(error.value)
