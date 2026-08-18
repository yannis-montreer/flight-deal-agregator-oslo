"""Tests de flightdeals.collectors.serpapi_client — HTTP mocke via httpx.MockTransport,
aucun appel reseau reel."""
from __future__ import annotations

import httpx
import pytest

from flightdeals.collectors.serpapi_client import QuotaExceededError, SerpApiClient, SerpApiError


def _client_with_transport(handler) -> SerpApiClient:
    client = SerpApiClient(api_key="test-key")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


class TestSearch:
    def test_successful_response_returns_json(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["api_key"] == "test-key"
            assert request.url.params["engine"] == "google_travel_explore"
            return httpx.Response(200, json={"destinations": [{"name": "Tokyo"}]})

        client = _client_with_transport(handler)
        data = client.search({"engine": "google_travel_explore"})
        assert data["destinations"][0]["name"] == "Tokyo"

    def test_429_raises_quota_exceeded_without_retry(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(429, text="quota exceeded")

        client = _client_with_transport(handler)
        with pytest.raises(QuotaExceededError):
            client.search({"engine": "google_travel_explore"})
        assert call_count == 1  # jamais de retry sur quota depasse

    def test_error_field_mentioning_quota_raises_quota_exceeded(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": "You have run out of searches for this month."})

        client = _client_with_transport(handler)
        with pytest.raises(QuotaExceededError):
            client.search({"engine": "google_travel_explore"})

    def test_other_error_field_raises_generic_serpapi_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": "Invalid parameter: foo"})

        client = _client_with_transport(handler)
        with pytest.raises(SerpApiError):
            client.search({"engine": "google_travel_explore"})

    def test_5xx_retries_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("flightdeals.collectors.serpapi_client.time.sleep", lambda _: None)
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return httpx.Response(500, text="server error")
            return httpx.Response(200, json={"destinations": []})

        client = _client_with_transport(handler)
        data = client.search({"engine": "google_travel_explore"})
        assert data == {"destinations": []}
        assert call_count == 3

    def test_5xx_persistent_raises_after_max_retries(self, monkeypatch):
        monkeypatch.setattr("flightdeals.collectors.serpapi_client.time.sleep", lambda _: None)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="down")

        client = _client_with_transport(handler)
        with pytest.raises(SerpApiError):
            client.search({"engine": "google_travel_explore"})

    def test_4xx_non_429_raises_immediately_without_retry(self):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(400, text="bad request")

        client = _client_with_transport(handler)
        with pytest.raises(SerpApiError):
            client.search({"engine": "google_travel_explore"})
        assert call_count == 1


class TestGetAccountInfo:
    def test_returns_parsed_json_without_consuming_quota_endpoint(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert "account.json" in str(request.url)
            return httpx.Response(200, json={"plan_searches_left": 200})

        client = _client_with_transport(handler)
        info = client.get_account_info()
        assert info["plan_searches_left"] == 200
