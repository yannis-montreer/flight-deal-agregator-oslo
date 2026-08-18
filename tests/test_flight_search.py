"""Tests de flightdeals.collectors.flight_search — sur la fixture issue du vrai Spike
(tests/fixtures/explore_response_sample.json) pour valider le parsing contre la forme reelle
de reponse SerpApi plutot qu'une supposition."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from flightdeals.collectors.flight_search import (
    _parse_explore_entry,
    fetch_all_explore_destinations,
    fetch_explore_destinations,
)
from flightdeals.collectors.serpapi_client import QuotaExceededError, SerpApiClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def explore_fixture() -> dict:
    return json.loads((FIXTURES_DIR / "explore_response_sample.json").read_text(encoding="utf-8"))


def _client_with_fixture_response(fixture: dict) -> SerpApiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture)

    client = SerpApiClient(api_key="test-key")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


class TestParseExploreEntry:
    def test_parses_priced_entry_from_real_fixture(self, explore_fixture):
        priced_entries = [d for d in explore_fixture["destinations"] if "flight_price" in d]
        assert priced_entries, "la fixture doit contenir au moins une entree avec prix"

        obs = _parse_explore_entry(priced_entries[0], origin="OSL", currency="NOK")
        assert obs.destination is not None
        assert obs.price is not None
        assert obs.is_exploitable

    def test_parses_unpriced_entry_as_non_exploitable_not_error(self, explore_fixture):
        unpriced_entries = [d for d in explore_fixture["destinations"] if "flight_price" not in d]
        if not unpriced_entries:
            pytest.skip("cette fixture n'a pas d'entree sans prix")

        obs = _parse_explore_entry(unpriced_entries[0], origin="OSL", currency="NOK")
        assert obs.price is None
        assert not obs.is_exploitable  # pas une exception, juste non exploitable

    def test_entirely_missing_price_and_airport_keys_does_not_crash(self):
        entry = {"name": "Nowhere", "start_date": "2026-10-01"}
        obs = _parse_explore_entry(entry, origin="OSL", currency="NOK")
        assert obs.destination is None
        assert obs.price is None
        assert not obs.is_exploitable

    def test_destination_airport_as_unexpected_type_does_not_crash(self):
        # garde-fou si le format SerpApi change un jour (scrape non contractuel, voir plan)
        entry = {"name": "Weird", "destination_airport": "STN"}  # str au lieu du dict attendu
        obs = _parse_explore_entry(entry, origin="OSL", currency="NOK")
        assert obs.destination is None


class TestFetchExploreDestinations:
    def test_returns_one_observation_per_destination(self, explore_fixture):
        client = _client_with_fixture_response(explore_fixture)
        observations = fetch_explore_destinations(client, origin="OSL", currency="NOK", travel_duration=2)
        assert len(observations) == len(explore_fixture["destinations"])

    def test_one_malformed_entry_does_not_abort_the_rest(self, explore_fixture):
        broken_fixture = {
            "destinations": [
                {"destination_airport": None, "flight_price": object()},  # non serialisable en pratique, mais...
                *explore_fixture["destinations"][:5],
            ]
        }
        # NB: object() ne passerait pas un vrai round-trip JSON ; on simule plutot une entree
        # dont un champ a un type inattendu, sans faire planter le parsing des suivantes.
        broken_fixture["destinations"][0] = {"destination_airport": {"code": None}, "flight_price": "not-a-number"}

        client = _client_with_fixture_response(broken_fixture)
        observations = fetch_explore_destinations(client, origin="OSL", currency="NOK", travel_duration=2)
        assert len(observations) == 6  # l'entree bizarre + les 5 normales, aucune n'est perdue


class TestFetchAllExploreDestinations:
    def test_quota_exceeded_propagates_and_stops_remaining_durations(self, explore_fixture):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(200, json=explore_fixture)
            return httpx.Response(429, text="quota")

        client = SerpApiClient(api_key="test-key")
        client._client = httpx.Client(transport=httpx.MockTransport(handler))

        with pytest.raises(QuotaExceededError):
            fetch_all_explore_destinations(client, origin="OSL", currency="NOK", travel_durations=[1, 2, 3])
        assert call_count == 2  # 1ere duree OK, 2e leve le quota -> propage, 3e jamais tentee

    def test_generic_error_on_one_duration_does_not_abort_others(self, explore_fixture):
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return httpx.Response(400, text="bad request for this duration")
            return httpx.Response(200, json=explore_fixture)

        client = SerpApiClient(api_key="test-key")
        client._client = httpx.Client(transport=httpx.MockTransport(handler))

        observations = fetch_all_explore_destinations(client, origin="OSL", currency="NOK", travel_durations=[1, 2, 3])
        assert call_count == 3  # les 3 durees sont tentees malgre l'echec de la 2e
        assert len(observations) == 2 * len(explore_fixture["destinations"])
