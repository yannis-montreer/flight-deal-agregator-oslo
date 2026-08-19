"""Tests de trip_watch (db + tracker). Suite plus legere que celle de flightdeals/ —
proportionnee a un outil temporaire et purpose-built, mais couvre la logique non-triviale
(rotation de duree sans etat, detection de nouveau minimum, isolation SerpApi/Telegram)."""
from __future__ import annotations

import re
from datetime import date

import httpx
import pytest

from trip_watch.db import get_connection, get_historical_min, insert_observation
from trip_watch.tracker import (
    DailyResult,
    departure_dates,
    run_daily_check,
    today_duration,
)


# ---------------------------------------------------------------------------
# db.py
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path / "trip_watch_test.db")
    yield connection
    connection.close()


def _obs(**overrides):
    defaults = dict(
        observed_at="2026-08-19T06:00:00+00:00", destination="SCL",
        departure_date="2027-01-15", return_date="2027-02-19",
        price=8000.0, currency="NOK", airline="LATAM", stops=1,
        price_level="typical", error=None,
    )
    defaults.update(overrides)
    return defaults


class TestDb:
    def test_insert_and_retrieve_min(self, conn):
        insert_observation(conn, **_obs(price=8000.0))
        insert_observation(conn, **_obs(price=7500.0))
        insert_observation(conn, **_obs(price=7800.0))

        assert get_historical_min(conn, "SCL", "2027-01-15", "2027-02-19") == 7500.0

    def test_min_scoped_to_exact_combination(self, conn):
        insert_observation(conn, **_obs(departure_date="2027-01-15", price=7500.0))
        insert_observation(conn, **_obs(departure_date="2027-01-16", price=6000.0))  # date differente

        assert get_historical_min(conn, "SCL", "2027-01-15", "2027-02-19") == 7500.0

    def test_min_ignores_null_prices(self, conn):
        insert_observation(conn, **_obs(price=None, error="aucun vol"))
        assert get_historical_min(conn, "SCL", "2027-01-15", "2027-02-19") is None

    def test_min_excludes_given_id(self, conn):
        first_id = insert_observation(conn, **_obs(price=7500.0))
        insert_observation(conn, **_obs(price=9000.0))

        # exclure la 1ere observation -> il ne reste que 9000
        assert get_historical_min(conn, "SCL", "2027-01-15", "2027-02-19", exclude_id=first_id) == 9000.0

    def test_no_prior_observations_returns_none(self, conn):
        assert get_historical_min(conn, "SCL", "2027-01-15", "2027-02-19") is None


# ---------------------------------------------------------------------------
# tracker.py — fonctions pures
# ---------------------------------------------------------------------------


class TestDepartureDates:
    def test_matches_friend_request_five_dates(self):
        dates = departure_dates(date(2027, 1, 15), 2)
        assert [d.isoformat() for d in dates] == [
            "2027-01-13", "2027-01-14", "2027-01-15", "2027-01-16", "2027-01-17",
        ]

    def test_zero_tolerance_returns_only_center(self):
        assert departure_dates(date(2027, 1, 15), 0) == [date(2027, 1, 15)]


class TestTodayDuration:
    def test_cycles_through_all_values_over_the_full_range(self):
        # 35 +/- 5 -> 11 valeurs (30..40) ; sur 11 jours consecutifs, chaque valeur doit
        # apparaitre exactement une fois (rotation complete, aucun etat necessaire)
        start = date(2027, 1, 1)
        seen = {today_duration(35, 5, date.fromordinal(start.toordinal() + i)) for i in range(11)}
        assert seen == set(range(30, 41))

    def test_deterministic_for_a_given_date(self):
        d = date(2027, 3, 10)
        assert today_duration(35, 5, d) == today_duration(35, 5, d)

    def test_zero_tolerance_always_returns_center(self):
        assert today_duration(35, 0, date(2027, 1, 1)) == 35
        assert today_duration(35, 0, date(2027, 6, 15)) == 35


# ---------------------------------------------------------------------------
# tracker.py — run_daily_check (integration : SerpApi + Telegram mockes, vraie SQLite)
# ---------------------------------------------------------------------------


class _FakeConfig:
    def __init__(self):
        self.origin = "OSL"
        self.destination = "SCL"
        self.destination_name = "Santiago"
        self.currency = "NOK"
        self.schedule = "06:00"
        self.departure_center = date(2027, 1, 15)
        self.departure_tolerance_days = 2
        self.duration_center_days = 35
        self.duration_tolerance_days = 5
        self.api_key = "test-key"
        self.telegram_bot_token = "test-token"
        self.telegram_chat_id = "12345"


def _flight_response(
    price: float, price_level: str = "typical", *, search_url: str | None = None, price_history=None
) -> dict:
    response = {
        "best_flights": [
            {
                "price": price,
                "flights": [
                    {"airline": "LATAM", "departure_airport": {}, "arrival_airport": {}},
                    {"airline": "LATAM", "departure_airport": {}, "arrival_airport": {}},
                ],
            }
        ],
        "price_insights": {"price_level": price_level, "lowest_price": price},
    }
    if search_url is not None:
        response["search_metadata"] = {"google_flights_url": search_url}
    if price_history is not None:
        response["price_insights"]["price_history"] = price_history
    return response


class TestRunDailyCheck:
    def test_full_run_stores_five_observations_and_sends_digest(self, tmp_path, monkeypatch):
        db_path = tmp_path / "trip_watch.db"

        def handler(request: httpx.Request) -> httpx.Response:
            # prix different par date de depart pour verifier que le "meilleur" choisi est bien le min
            price = {"2027-01-13": 9000, "2027-01-14": 8500, "2027-01-15": 7000,
                      "2027-01-16": 8800, "2027-01-17": 9200}[request.url.params["outbound_date"]]
            return httpx.Response(200, json=_flight_response(price))

        # tracker.py appelle client.search(params) sur ce que retourne SerpApiClient(...),
        # utilise en context manager -> petit adaptateur autour d'un httpx.Client mocke.
        class _ClientAdapter:
            def __init__(self, transport_client):
                self._client = transport_client

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def search(self, params):
                response = self._client.get("https://serpapi.com/search.json", params=params)
                return response.json()

        monkeypatch.setattr(
            "trip_watch.tracker.SerpApiClient",
            lambda api_key: _ClientAdapter(httpx.Client(transport=httpx.MockTransport(handler))),
        )

        sent_messages = []
        monkeypatch.setattr(
            "trip_watch.tracker.send_message",
            lambda token, chat_id, text: sent_messages.append(text),
        )

        run_daily_check(_FakeConfig(), db_path)

        conn = get_connection(db_path)
        count = conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()["n"]
        conn.close()
        assert count == 5  # les 5 dates de depart, toutes stockees

        assert len(sent_messages) == 1
        assert "15/01/2027" in sent_messages[0]  # la date au prix le plus bas (7000), format jj/mm/aaaa
        assert "7 000 NOK" in sent_messages[0]
        assert "NOUVEAU MINIMUM" in sent_messages[0]  # 1ere observation -> forcement un minimum

    def test_second_run_with_higher_price_is_not_flagged_as_new_minimum(self, tmp_path, monkeypatch):
        db_path = tmp_path / "trip_watch.db"

        def make_handler(price):
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=_flight_response(price))
            return handler

        class _ClientAdapter:
            def __init__(self, handler):
                self._client = httpx.Client(transport=httpx.MockTransport(handler))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def search(self, params):
                return self._client.get("https://serpapi.com/search.json", params=params).json()

        sent_messages = []
        monkeypatch.setattr("trip_watch.tracker.send_message", lambda token, chat_id, text: sent_messages.append(text))

        # 1er run : prix bas (7000) -> nouveau minimum partout
        monkeypatch.setattr("trip_watch.tracker.SerpApiClient", lambda api_key: _ClientAdapter(make_handler(7000)))
        run_daily_check(_FakeConfig(), db_path)

        # 2e run : prix plus haut (7500) -> ne doit PAS etre signale comme nouveau minimum
        monkeypatch.setattr("trip_watch.tracker.SerpApiClient", lambda api_key: _ClientAdapter(make_handler(7500)))
        run_daily_check(_FakeConfig(), db_path)

        assert len(sent_messages) == 2
        assert "NOUVEAU MINIMUM" not in sent_messages[1]

    def test_serpapi_error_on_one_date_does_not_abort_the_others(self, tmp_path, monkeypatch):
        db_path = tmp_path / "trip_watch.db"
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(400, text="bad request")
            return httpx.Response(200, json=_flight_response(8000))

        class _ClientAdapter:
            def __init__(self, api_key):
                self._client = httpx.Client(transport=httpx.MockTransport(handler))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def search(self, params):
                response = self._client.get("https://serpapi.com/search.json", params=params)
                if response.status_code >= 400:
                    from flightdeals.collectors.serpapi_client import SerpApiError
                    raise SerpApiError(f"HTTP {response.status_code}")
                return response.json()

        monkeypatch.setattr("trip_watch.tracker.SerpApiClient", _ClientAdapter)
        sent_messages = []
        monkeypatch.setattr("trip_watch.tracker.send_message", lambda token, chat_id, text: sent_messages.append(text))

        run_daily_check(_FakeConfig(), db_path)

        conn = get_connection(db_path)
        rows = conn.execute("SELECT price, error FROM observations").fetchall()
        conn.close()
        assert len(rows) == 5  # toutes stockees, y compris celle en erreur
        assert sum(1 for r in rows if r["error"] is not None) == 1
        assert sum(1 for r in rows if r["price"] is not None) == 4
        assert len(sent_messages) == 1  # le digest part quand meme, base sur les 4 valides

    def test_account_quota_included_in_digest_when_available(self, tmp_path, monkeypatch):
        """Demande utilisateur : visibilite sur le quota d'un compte SerpApi tiers (pas
        d'acces a son dashboard) — account.json est gratuit, affiche dans le digest quotidien."""
        db_path = tmp_path / "trip_watch.db"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_flight_response(8000))

        class _ClientAdapterWithAccountInfo:
            def __init__(self, transport_client):
                self._client = transport_client

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_account_info(self):
                return {"total_searches_left": 842, "this_month_usage": 158}

            def search(self, params):
                return self._client.get("https://serpapi.com/search.json", params=params).json()

        monkeypatch.setattr(
            "trip_watch.tracker.SerpApiClient",
            lambda api_key: _ClientAdapterWithAccountInfo(httpx.Client(transport=httpx.MockTransport(handler))),
        )
        sent_messages = []
        monkeypatch.setattr("trip_watch.tracker.send_message", lambda token, chat_id, text: sent_messages.append(text))

        run_daily_check(_FakeConfig(), db_path)

        assert len(sent_messages) == 1
        assert "842" in sent_messages[0]
        assert "Quota SerpApi restant" in sent_messages[0]

    def test_account_info_failure_does_not_block_the_digest(self, tmp_path, monkeypatch):
        db_path = tmp_path / "trip_watch.db"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_flight_response(8000))

        class _ClientAdapterBrokenAccountInfo:
            def __init__(self, transport_client):
                self._client = transport_client

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_account_info(self):
                raise RuntimeError("panne simulee account.json")

            def search(self, params):
                return self._client.get("https://serpapi.com/search.json", params=params).json()

        monkeypatch.setattr(
            "trip_watch.tracker.SerpApiClient",
            lambda api_key: _ClientAdapterBrokenAccountInfo(httpx.Client(transport=httpx.MockTransport(handler))),
        )
        sent_messages = []
        monkeypatch.setattr("trip_watch.tracker.send_message", lambda token, chat_id, text: sent_messages.append(text))

        run_daily_check(_FakeConfig(), db_path)

        assert len(sent_messages) == 1  # le digest part quand meme
        assert "Quota SerpApi" not in sent_messages[0]  # juste omis, pas de crash

    def test_dates_rendered_as_dd_mm_yyyy_not_iso(self, tmp_path, monkeypatch):
        db_path = tmp_path / "trip_watch.db"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_flight_response(8000))

        class _ClientAdapter:
            def __init__(self, api_key):
                self._client = httpx.Client(transport=httpx.MockTransport(handler))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def search(self, params):
                return self._client.get("https://serpapi.com/search.json", params=params).json()

        monkeypatch.setattr("trip_watch.tracker.SerpApiClient", _ClientAdapter)
        sent_messages = []
        monkeypatch.setattr("trip_watch.tracker.send_message", lambda token, chat_id, text: sent_messages.append(text))

        run_daily_check(_FakeConfig(), db_path)

        # prix identique sur les 5 dates -> peu importe laquelle "gagne", seul le FORMAT compte ici
        assert re.search(r"\b\d{2}/\d{2}/2027\b", sent_messages[0])
        assert not re.search(r"\b2027-\d{2}-\d{2}\b", sent_messages[0])  # plus de format ISO brut

    def test_search_url_included_when_present(self, tmp_path, monkeypatch):
        db_path = tmp_path / "trip_watch.db"
        url = "https://www.google.com/travel/flights?curr=NOK&tfs=example"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_flight_response(8000, search_url=url))

        class _ClientAdapter:
            def __init__(self, api_key):
                self._client = httpx.Client(transport=httpx.MockTransport(handler))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def search(self, params):
                return self._client.get("https://serpapi.com/search.json", params=params).json()

        monkeypatch.setattr("trip_watch.tracker.SerpApiClient", _ClientAdapter)
        sent_messages = []
        monkeypatch.setattr("trip_watch.tracker.send_message", lambda token, chat_id, text: sent_messages.append(text))

        run_daily_check(_FakeConfig(), db_path)

        assert url in sent_messages[0]
        assert "Voir sur Google Flights" in sent_messages[0]

    def test_search_url_omitted_when_absent(self, tmp_path, monkeypatch):
        db_path = tmp_path / "trip_watch.db"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_flight_response(8000))  # pas de search_metadata

        class _ClientAdapter:
            def __init__(self, api_key):
                self._client = httpx.Client(transport=httpx.MockTransport(handler))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def search(self, params):
                return self._client.get("https://serpapi.com/search.json", params=params).json()

        monkeypatch.setattr("trip_watch.tracker.SerpApiClient", _ClientAdapter)
        sent_messages = []
        monkeypatch.setattr("trip_watch.tracker.send_message", lambda token, chat_id, text: sent_messages.append(text))

        run_daily_check(_FakeConfig(), db_path)

        assert "Voir sur Google Flights" not in sent_messages[0]

    def test_typical_avg_price_shown_alongside_price_level(self, tmp_path, monkeypatch):
        db_path = tmp_path / "trip_watch.db"
        history = [[1781820000, 8000], [1781906400, 9000], [1781992800, 10000]]  # moyenne = 9000

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_flight_response(8000, price_history=history))

        class _ClientAdapter:
            def __init__(self, api_key):
                self._client = httpx.Client(transport=httpx.MockTransport(handler))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def search(self, params):
                return self._client.get("https://serpapi.com/search.json", params=params).json()

        monkeypatch.setattr("trip_watch.tracker.SerpApiClient", _ClientAdapter)
        sent_messages = []
        monkeypatch.setattr("trip_watch.tracker.send_message", lambda token, chat_id, text: sent_messages.append(text))

        run_daily_check(_FakeConfig(), db_path)

        assert "Evaluation Google : prix typical (moyenne habituelle ~9 000 NOK)" in sent_messages[0]

    def test_typical_avg_price_omitted_without_history(self, tmp_path, monkeypatch):
        db_path = tmp_path / "trip_watch.db"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_flight_response(8000))  # pas de price_history

        class _ClientAdapter:
            def __init__(self, api_key):
                self._client = httpx.Client(transport=httpx.MockTransport(handler))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def search(self, params):
                return self._client.get("https://serpapi.com/search.json", params=params).json()

        monkeypatch.setattr("trip_watch.tracker.SerpApiClient", _ClientAdapter)
        sent_messages = []
        monkeypatch.setattr("trip_watch.tracker.send_message", lambda token, chat_id, text: sent_messages.append(text))

        run_daily_check(_FakeConfig(), db_path)

        assert "moyenne habituelle" not in sent_messages[0]
        assert "Evaluation Google : prix typical" in sent_messages[0]

    def test_malformed_price_history_entries_are_ignored_not_fatal(self, tmp_path, monkeypatch):
        db_path = tmp_path / "trip_watch.db"
        # 2 entrees invalides (prix non numerique, entree tronquee) + 1 valide -> ne doit pas planter
        history = [[1781820000, "N/A"], [1781906400], [1781992800, 10000]]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_flight_response(8000, price_history=history))

        class _ClientAdapter:
            def __init__(self, api_key):
                self._client = httpx.Client(transport=httpx.MockTransport(handler))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def search(self, params):
                return self._client.get("https://serpapi.com/search.json", params=params).json()

        monkeypatch.setattr("trip_watch.tracker.SerpApiClient", _ClientAdapter)
        sent_messages = []
        monkeypatch.setattr("trip_watch.tracker.send_message", lambda token, chat_id, text: sent_messages.append(text))

        run_daily_check(_FakeConfig(), db_path)  # ne doit pas lever

        assert "moyenne habituelle ~10 000 NOK" in sent_messages[0]  # seule l'entree valide comptee
