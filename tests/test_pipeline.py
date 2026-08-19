"""Tests d'integration de flightdeals.pipeline.run_once — SerpApi et Telegram mockes, vraie
SQLite temporaire (pas de mock DB : c'est l'interaction reelle entre les couches qu'on veut
valider, pas juste chaque couche isolement)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from flightdeals.config import (
    CollectionConfig,
    Config,
    DealConfig,
    DedupConfig,
    LoggingConfig,
    NotificationConfig,
    ScoringConfig,
    Secrets,
    SerpApiConfig,
)
from flightdeals.db.connection import get_connection
from flightdeals.db.repository import FlightObservation, insert_observation, increment_requests_used
from flightdeals.pipeline import run_once


def _make_config(**overrides) -> Config:
    defaults = dict(
        origin="OSL",
        currency="NOK",
        collection=CollectionConfig(enabled=True, schedule="02:00"),
        serpapi=SerpApiConfig(
            monthly_budget=200, min_budget_reserve=5, travel_durations=(1, 2, 3),
            arrival_area_id=None, month=None,
        ),
        deal=DealConfig(
            minimum_discount=0.30,
            minimum_observations=7,
            percentile_threshold=0.25,
            history_window_days=30,
            duration_tolerance_nights=2,
            max_duration_deviation_ratio=0.5,
            # Fourchette large par defaut (les observations de test font 7 nuits) : la
            # plupart des tests de ce fichier ne testent pas ce filtre specifiquement -
            # voir TestTripLengthFilter plus bas pour les tests dedies avec 6/14 explicites.
            min_trip_length_nights=0,
            max_trip_length_nights=9999,
        ),
        scoring=ScoringConfig(
            weights={"discount": 0.45, "percentile": 0.25, "directness": 0.10, "confidence": 0.20},
            discount_cap=0.60,
            confidence_saturation_count=30,
            directness_bonus={"nonstop": 1.0, "one_stop": 0.5, "multi_stop": 0.0, "unknown": 0.5},
        ),
        dedup=DedupConfig(further_drop_threshold=0.10, reappear_gap_days=14),
        # daily_summary_enabled=False par defaut ICI (contrairement a config.yaml en prod,
        # ou c'est True) : la plupart des tests de ce fichier verifient des notifications de
        # deal precises via sent_messages, et n'ont pas a se soucier du recap quotidien —
        # voir TestDailySummary plus bas pour les tests dedies a cette fonctionnalite.
        notification=NotificationConfig(telegram=True, send_delay_seconds=0.0, daily_summary_enabled=False),
        logging=LoggingConfig(level="INFO", max_bytes=1_000_000, backup_count=1),
        secrets=Secrets(serpapi_key="test-key", telegram_bot_token="test-token", telegram_chat_id="12345"),
    )
    defaults.update(overrides)
    return Config(**defaults)


class _FakeSerpApiClient:
    """Double de test pour SerpApiClient : aucun appel reseau, reponses controlees a l'avance."""

    def __init__(self, account_info=None, search_responses=None):
        self.account_info = account_info if account_info is not None else {"total_searches_left": 200}
        self._search_responses = list(search_responses) if search_responses else []
        self.search_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def close(self):
        pass

    def get_account_info(self):
        return self.account_info

    def search(self, params):
        self.search_calls += 1
        if not self._search_responses:
            return {"destinations": []}
        response = self._search_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _install_fake_serpapi(monkeypatch, fake_client: _FakeSerpApiClient) -> None:
    monkeypatch.setattr("flightdeals.pipeline.SerpApiClient", lambda api_key: fake_client)


def _install_fake_telegram(monkeypatch, sent_messages: list) -> None:
    def fake_post(url, data=None, timeout=None):
        sent_messages.append(data)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("flightdeals.notify.telegram.httpx.post", fake_post)


def _explore_response(destinations: list) -> dict:
    return {"destinations": destinations}


def _destination(**overrides) -> dict:
    defaults = dict(
        destination_airport={"code": "NRT"},
        name="Tokyo",
        start_date="2027-01-12",
        end_date="2027-01-19",
        flight_price=3000,
        flight_duration=900,
        number_of_stops=0,
        airline="SAS",
        link="https://example.com/flight",
    )
    defaults.update(overrides)
    return defaults


def _seed_history(conn, *, count: int, price: float, observed_days_ago: int, **overrides) -> None:
    """Insere `count` observations historiques OSL->NRT dans le bucket que les tests
    utilisent (2027-01, 7 nuits, nonstop), a un prix donne."""
    observed_at = (datetime.now(timezone.utc) - timedelta(days=observed_days_ago)).isoformat()
    defaults = dict(
        observed_at=observed_at,
        origin="OSL",
        destination="NRT",
        destination_name="Tokyo",
        departure_date="2027-01-12",
        return_date="2027-01-19",
        price=price,
        currency="NOK",
        airline="SAS",
        stops=0,
        duration_minutes=900,
        source="serpapi_google_travel_explore",
        source_url="https://example.com",
        trip_length_nights=7,
        travel_period_bucket="2027-01",
        stops_bucket="nonstop",
    )
    defaults.update(overrides)
    for _ in range(count):
        insert_observation(conn, FlightObservation(**defaults))


class TestRunOnceBudget:
    def test_skips_entirely_when_local_budget_exhausted(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        increment_requests_used(conn, datetime.now(timezone.utc).strftime("%Y-%m"), count=198)
        conn.close()

        fake_client = _FakeSerpApiClient(account_info={"total_searches_left": 200})
        _install_fake_serpapi(monkeypatch, fake_client)

        summary = run_once(_make_config(), db_path)  # monthly_budget=200, 198 deja utilises -> reste 2 < reserve 5

        assert summary.skipped_budget is True
        assert fake_client.search_calls == 0

    def test_skips_when_real_serpapi_budget_low(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        fake_client = _FakeSerpApiClient(account_info={"total_searches_left": 2})  # < min_budget_reserve=5
        _install_fake_serpapi(monkeypatch, fake_client)

        summary = run_once(_make_config(), db_path)

        assert summary.skipped_budget is True
        assert fake_client.search_calls == 0

    def test_proceeds_normally_when_budget_healthy(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[_explore_response([]), _explore_response([]), _explore_response([])],
        )
        _install_fake_serpapi(monkeypatch, fake_client)

        summary = run_once(_make_config(), db_path)

        assert summary.skipped_budget is False
        assert fake_client.search_calls == 3


class TestRunOnceHappyPath:
    def test_triggers_and_notifies_a_genuine_deal(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        _seed_history(conn, count=7, price=5000.0, observed_days_ago=5)
        conn.close()

        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([_destination(flight_price=3000)]),
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client)
        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        summary = run_once(_make_config(), db_path)

        assert summary.skipped_budget is False
        assert summary.observations_collected == 1
        assert summary.observations_stored == 1
        assert summary.deals_triggered == 1
        assert summary.deals_notified == 1
        assert len(sent_messages) == 1
        assert "Tokyo" in sent_messages[0]["text"]

        conn = get_connection(db_path)
        obs_count = conn.execute("SELECT COUNT(*) AS n FROM flight_observations").fetchone()["n"]
        notif_count = conn.execute("SELECT COUNT(*) AS n FROM notified_deals").fetchone()["n"]
        conn.close()
        assert obs_count == 8  # 7 historiques deja en base + 1 nouvelle
        assert notif_count == 1

    def test_insufficient_history_stores_but_does_not_trigger(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        _seed_history(conn, count=3, price=5000.0, observed_days_ago=5)  # < minimum_observations=7
        conn.close()

        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([_destination(flight_price=1000)]),  # discount enorme, mais historique trop court
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client)
        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        summary = run_once(_make_config(), db_path)

        assert summary.observations_stored == 1  # toujours stockee...
        assert summary.deals_triggered == 0  # ...mais pas de deal (historique < 7)
        assert summary.deals_notified == 0
        assert sent_messages == []

    def test_unpriced_destination_is_skipped_not_stored(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        # entree sans flight_price du tout (frequent sur le bucket ~2 semaines, voir Spike)
        unpriced = {"destination_airport": {"code": "FCO"}, "name": "Rome", "start_date": "2027-01-14"}

        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[_explore_response([unpriced]), _explore_response([]), _explore_response([])],
        )
        _install_fake_serpapi(monkeypatch, fake_client)
        _install_fake_telegram(monkeypatch, [])

        summary = run_once(_make_config(), db_path)

        assert summary.observations_collected == 1
        assert summary.observations_stored == 0  # non exploitable, jamais stockee


class TestRunOnceDurationFilter:
    def test_excludes_deal_with_abnormally_long_duration_despite_great_price(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        # historique : prix eleve (5000) ET duree normale (900min = 15h) repetee 7 fois
        _seed_history(conn, count=7, price=5000.0, observed_days_ago=5, duration_minutes=900)
        conn.close()

        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                # prix imbattable (3000, -40%) MAIS duree 1500min (25h) = tres au-dela de
                # 900 * 1.5 = 1350min (seuil par defaut max_duration_deviation_ratio=0.5)
                _explore_response([_destination(flight_price=3000, flight_duration=1500)]),
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client)

        summary = run_once(_make_config(), db_path)

        assert summary.observations_stored == 1  # observation quand meme stockee
        assert summary.deals_triggered == 0  # ...mais exclue (duree anormale)
        assert summary.deals_notified == 0
        assert sent_messages == []

    def test_similar_duration_still_triggers_normally(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        _seed_history(conn, count=7, price=5000.0, observed_days_ago=5, duration_minutes=900)
        conn.close()

        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                # duree tres proche de l'historique (920 vs moyenne 900) -> ne doit pas exclure
                _explore_response([_destination(flight_price=3000, flight_duration=920)]),
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client)

        summary = run_once(_make_config(), db_path)

        assert summary.deals_triggered == 1
        assert summary.deals_notified == 1
        assert len(sent_messages) == 1

    def test_missing_duration_data_does_not_block_a_deal(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        _seed_history(conn, count=7, price=5000.0, observed_days_ago=5, duration_minutes=900)
        conn.close()

        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        destination_without_duration = _destination(flight_price=3000)
        del destination_without_duration["flight_duration"]

        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([destination_without_duration]),
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client)

        summary = run_once(_make_config(), db_path)

        assert summary.deals_triggered == 1  # duree absente -> filtre ne bloque pas (fail-open)
        assert summary.deals_notified == 1


def _config_with_trip_length_range(min_nights: int, max_nights: int) -> Config:
    return _make_config(
        deal=DealConfig(
            minimum_discount=0.30, minimum_observations=7, percentile_threshold=0.25,
            history_window_days=30, duration_tolerance_nights=2, max_duration_deviation_ratio=0.5,
            min_trip_length_nights=min_nights, max_trip_length_nights=max_nights,
        )
    )


class TestRunOnceTripLengthFilter:
    """min_trip_length_nights=6, max_trip_length_nights=14 (valeurs demandees par
    l'utilisateur) : exclut les weekends (~2 nuits) et les longs sejours (~1 mois)."""

    def test_weekend_trip_excluded_despite_great_price(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        # historique sur le MEME sejour court (2 nuits) pour que le seul obstacle soit la duree
        _seed_history(conn, count=7, price=5000.0, observed_days_ago=5,
                       return_date="2027-01-14", trip_length_nights=2)
        conn.close()

        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([_destination(flight_price=3000, end_date="2027-01-14")]),  # 2 nuits
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client)
        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        summary = run_once(_config_with_trip_length_range(6, 14), db_path)

        assert summary.observations_stored == 1  # stockee quand meme
        assert summary.deals_triggered == 0  # ...mais exclue (2 nuits < minimum 6)
        assert sent_messages == []

    def test_month_long_trip_excluded_despite_great_price(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        _seed_history(conn, count=7, price=5000.0, observed_days_ago=5,
                       return_date="2027-02-11", trip_length_nights=30)
        conn.close()

        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([_destination(flight_price=3000, end_date="2027-02-11")]),  # 30 nuits
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client)
        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        summary = run_once(_config_with_trip_length_range(6, 14), db_path)

        assert summary.observations_stored == 1
        assert summary.deals_triggered == 0  # 30 nuits > maximum 14
        assert sent_messages == []

    def test_trip_within_range_triggers_normally(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        _seed_history(conn, count=7, price=5000.0, observed_days_ago=5)  # 7 nuits par defaut, dans [6,14]
        conn.close()

        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([_destination(flight_price=3000)]),  # 7 nuits par defaut
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client)
        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        summary = run_once(_config_with_trip_length_range(6, 14), db_path)

        assert summary.deals_triggered == 1
        assert summary.deals_notified == 1
        assert len(sent_messages) == 1


class TestRunOnceErrorIsolation:
    def test_failure_on_one_observation_does_not_abort_the_run(self, tmp_path, monkeypatch):
        """Simule une panne (ex: verrou DB transitoire) sur la 1ere observation traitee :
        la 2e doit quand meme etre traitee et stockee normalement."""
        db_path = tmp_path / "test.db"

        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([_destination(destination_airport={"code": "NRT"}), _destination(destination_airport={"code": "BKK"}, name="Bangkok")]),
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client)
        _install_fake_telegram(monkeypatch, [])

        import flightdeals.pipeline as pipeline_module

        real_insert = pipeline_module.insert_observation
        call_count = 0

        def flaky_insert(conn, obs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("panne simulee sur la 1ere observation")
            return real_insert(conn, obs)

        monkeypatch.setattr("flightdeals.pipeline.insert_observation", flaky_insert)

        summary = run_once(_make_config(), db_path)

        assert summary.observations_collected == 2
        assert summary.observations_stored == 1  # la 1ere a echoue, la 2e a quand meme reussi

    def test_scoring_failure_still_keeps_the_observation_stored(self, tmp_path, monkeypatch):
        """La donnee est deja stockee de facon durable AVANT l'evaluation (voir pipeline.py) :
        un bug de scoring sur une observation precise ne doit pas faire perdre son stockage."""
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        _seed_history(conn, count=7, price=5000.0, observed_days_ago=5)
        conn.close()

        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([_destination(flight_price=3000)]),
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client)
        _install_fake_telegram(monkeypatch, [])

        monkeypatch.setattr(
            "flightdeals.pipeline.evaluate_deal",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bug de scoring simule")),
        )

        summary = run_once(_make_config(), db_path)

        assert summary.observations_stored == 1  # stockee malgre l'echec du scoring
        assert summary.deals_triggered == 0  # aucun deal (le scoring a echoue, traite comme "pas de deal")

        conn = get_connection(db_path)
        obs_count = conn.execute("SELECT COUNT(*) AS n FROM flight_observations").fetchone()["n"]
        conn.close()
        assert obs_count == 8  # 7 historiques + 1 nouvelle, malgre l'echec du scoring dessus


class TestRunOnceDedup:
    def test_same_deal_not_notified_twice_in_a_row(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        _seed_history(conn, count=7, price=5000.0, observed_days_ago=10)
        conn.close()

        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        # 1er run : historique suffisant, gros discount -> doit notifier
        fake_client_1 = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([_destination(flight_price=3000)]),
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client_1)
        summary_1 = run_once(_make_config(), db_path)
        assert summary_1.deals_notified == 1

        # 2e run, memes donnees (meme prix) : se declenche a nouveau mais dedup doit suppresser
        fake_client_2 = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([_destination(flight_price=3000)]),
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client_2)
        summary_2 = run_once(_make_config(), db_path)

        assert summary_2.deals_triggered == 1  # se declenche toujours statistiquement...
        assert summary_2.deals_notified == 0  # ...mais supprime par dedoublonnage
        assert len(sent_messages) == 1  # un seul message envoye au total sur les 2 runs

    def test_further_price_drop_notifies_again(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        _seed_history(conn, count=7, price=5000.0, observed_days_ago=10)
        conn.close()

        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        fake_client_1 = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([_destination(flight_price=3000)]),
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client_1)
        run_once(_make_config(), db_path)

        # 2e run : prix encore 15% plus bas (2550 = 3000 * 0.85) -> re-notification attendue
        fake_client_2 = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([_destination(flight_price=2550)]),
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client_2)
        summary_2 = run_once(_make_config(), db_path)

        assert summary_2.deals_notified == 1


class TestDailySummary:
    def test_sent_after_a_normal_run_with_no_deal(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[_explore_response([]), _explore_response([]), _explore_response([])],
        )
        _install_fake_serpapi(monkeypatch, fake_client)
        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        config = _make_config(notification=NotificationConfig(telegram=True, send_delay_seconds=0.0, daily_summary_enabled=True))
        run_once(config, db_path)

        assert len(sent_messages) == 1
        text = sent_messages[0]["text"]
        assert "Recap quotidien" in text
        assert "0 deal(s) detecte" in text
        assert "3/200 requetes" in text  # 3 durees interrogees

    def test_sent_in_addition_to_deal_notification_when_a_deal_triggers(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = get_connection(db_path)
        _seed_history(conn, count=7, price=5000.0, observed_days_ago=5)
        conn.close()

        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[
                _explore_response([_destination(flight_price=3000)]),
                _explore_response([]),
                _explore_response([]),
            ],
        )
        _install_fake_serpapi(monkeypatch, fake_client)
        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        config = _make_config(notification=NotificationConfig(telegram=True, send_delay_seconds=0.0, daily_summary_enabled=True))
        run_once(config, db_path)

        # 1 notification de deal (format DealMessage) + 1 recap quotidien = 2 messages distincts
        assert len(sent_messages) == 2
        assert "FLIGHT DEAL" in sent_messages[0]["text"]
        assert "Recap quotidien" in sent_messages[1]["text"]
        assert "1 deal(s) detecte" in sent_messages[1]["text"]

    def test_sent_even_when_budget_is_skipped(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        fake_client = _FakeSerpApiClient(account_info={"total_searches_left": 2})  # < min_budget_reserve=5
        _install_fake_serpapi(monkeypatch, fake_client)
        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        config = _make_config(notification=NotificationConfig(telegram=True, send_delay_seconds=0.0, daily_summary_enabled=True))
        summary = run_once(config, db_path)

        assert summary.skipped_budget is True
        assert len(sent_messages) == 1
        assert "saute" in sent_messages[0]["text"]

    def test_disabled_by_config_sends_nothing_extra(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        fake_client = _FakeSerpApiClient(
            account_info={"total_searches_left": 200},
            search_responses=[_explore_response([]), _explore_response([]), _explore_response([])],
        )
        _install_fake_serpapi(monkeypatch, fake_client)
        sent_messages: list = []
        _install_fake_telegram(monkeypatch, sent_messages)

        run_once(_make_config(), db_path)  # daily_summary_enabled=False par defaut dans _make_config

        assert sent_messages == []
