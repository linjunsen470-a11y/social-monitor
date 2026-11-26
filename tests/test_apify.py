from catering_watch.apify import ApifyClient

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
