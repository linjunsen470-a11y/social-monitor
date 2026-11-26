import pytest
import httpx

from catering_watch.apify import ApifyClient, ApifyError, StartRejected


def test_start_and_page_use_auth_cap_and_physical_offsets(monkeypatch):
    def handle(request):
        assert request.headers["Authorization"] == "Bearer test"
        if request.method == "POST":
            assert request.url.path.endswith("/acts/vendor~actor/runs")
            assert request.url.params["maxTotalChargeUsd"] == "0.1000"
            assert request.url.params["build"] == "latest"
            return httpx.Response(201, json={"data": {"id": "run1"}})
        assert request.url.path.endswith("/datasets/dataset1/items")
        assert dict(request.url.params) == {"offset": "100", "limit": "100", "skipHidden": "true"}
        return httpx.Response(200, json=[{"note_id": "abc"}])

    client_type = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw)
    )
    client = ApifyClient(token="test")
    assert client.start("vendor/actor", {"max_items": 1}, 0.1, "latest")["id"] == "run1"
    assert client.page("dataset1", 100, 100) == [{"note_id": "abc"}]


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
