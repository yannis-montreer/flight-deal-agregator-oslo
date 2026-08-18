"""Tests de flightdeals.db.repository."""
from __future__ import annotations

from flightdeals.db.connection import get_connection
from flightdeals.db.repository import (
    FlightObservation,
    NotifiedDeal,
    get_last_notification,
    get_requests_used,
    increment_requests_used,
    insert_notification,
    insert_observation,
    query_comparable_prices,
)


def _obs(**overrides) -> FlightObservation:
    defaults = dict(
        observed_at="2026-08-01T02:00:00+00:00",
        origin="OSL",
        destination="NRT",
        destination_name="Tokyo",
        departure_date="2027-01-12",
        return_date="2027-01-19",
        price=5000.0,
        currency="NOK",
        airline="SAS",
        stops=1,
        duration_minutes=900,
        source="serpapi_google_travel_explore",
        source_url="https://example.com",
        trip_length_nights=7,
        travel_period_bucket="2027-01",
        stops_bucket="one_stop",
    )
    defaults.update(overrides)
    return FlightObservation(**defaults)


def _deal(**overrides) -> NotifiedDeal:
    defaults = dict(
        notified_at="2026-08-18T02:05:00+00:00",
        origin="OSL",
        destination="NRT",
        departure_date="2027-01-12",
        return_date="2027-01-19",
        price=3200.0,
        currency="NOK",
        score=87,
        discount=0.4,
        observation_id=1,
    )
    defaults.update(overrides)
    return NotifiedDeal(**defaults)


class TestInsertObservation:
    def test_insert_returns_id_and_persists(self, conn):
        obs_id = insert_observation(conn, _obs())
        assert obs_id is not None

        row = conn.execute("SELECT * FROM flight_observations WHERE id = ?", (obs_id,)).fetchone()
        assert row["origin"] == "OSL"
        assert row["destination"] == "NRT"
        assert row["price"] == 5000.0
        assert row["trip_length_nights"] == 7

    def test_one_way_flight_allows_null_return_date_and_nights(self, conn):
        obs_id = insert_observation(conn, _obs(return_date=None, trip_length_nights=None))
        row = conn.execute("SELECT * FROM flight_observations WHERE id = ?", (obs_id,)).fetchone()
        assert row["return_date"] is None
        assert row["trip_length_nights"] is None

    def test_schema_reapplication_is_idempotent_and_preserves_data(self, tmp_path):
        db_path = tmp_path / "idempotent.db"
        conn1 = get_connection(db_path)
        insert_observation(conn1, _obs())
        conn1.close()

        # Rouvrir re-execute schema.sql (CREATE TABLE IF NOT EXISTS) : ne doit ni planter
        # ni effacer les donnees existantes.
        conn2 = get_connection(db_path)
        count = conn2.execute("SELECT COUNT(*) AS n FROM flight_observations").fetchone()["n"]
        assert count == 1
        conn2.close()


class TestQueryComparablePrices:
    def test_matches_same_route_bucket_and_duration_window(self, conn):
        for price, nights in [(4900, 6), (5200, 7), (5400, 8)]:
            insert_observation(
                conn, _obs(price=price, trip_length_nights=nights, observed_at="2026-07-15T02:00:00+00:00")
            )
        # hors fenetre de duree (21 nuits, cf. exemple du spec) -> ne doit pas matcher
        insert_observation(
            conn, _obs(price=1000, trip_length_nights=21, observed_at="2026-07-15T02:00:00+00:00")
        )
        # autre destination -> ne doit pas matcher
        insert_observation(conn, _obs(destination="BKK", price=999, observed_at="2026-07-15T02:00:00+00:00"))

        prices = query_comparable_prices(
            conn,
            origin="OSL",
            destination="NRT",
            currency="NOK",
            travel_period_bucket="2027-01",
            trip_length_nights=7,
            duration_tolerance_nights=2,
            stops_bucket=None,
            window_start="2026-07-01T00:00:00+00:00",
            upper_bound="2026-08-18T00:00:00+00:00",
        )
        assert prices == [4900, 5200, 5400]

    def test_excludes_observations_at_or_after_upper_bound(self, conn):
        """Anti-auto-comparaison : upper_bound = debut du run courant, jamais 'maintenant'."""
        insert_observation(conn, _obs(price=5000, observed_at="2026-08-18T01:00:00+00:00"))
        insert_observation(conn, _obs(price=6000, observed_at="2026-08-18T03:00:00+00:00"))

        prices = query_comparable_prices(
            conn,
            origin="OSL",
            destination="NRT",
            currency="NOK",
            travel_period_bucket="2027-01",
            trip_length_nights=7,
            duration_tolerance_nights=2,
            stops_bucket=None,
            window_start="2026-07-01T00:00:00+00:00",
            upper_bound="2026-08-18T02:00:00+00:00",
        )
        assert prices == [5000]

    def test_excludes_observations_before_window_start(self, conn):
        insert_observation(conn, _obs(price=5000, observed_at="2026-06-01T00:00:00+00:00"))  # trop vieux
        insert_observation(conn, _obs(price=6000, observed_at="2026-07-15T00:00:00+00:00"))

        prices = query_comparable_prices(
            conn,
            origin="OSL",
            destination="NRT",
            currency="NOK",
            travel_period_bucket="2027-01",
            trip_length_nights=7,
            duration_tolerance_nights=2,
            stops_bucket=None,
            window_start="2026-07-01T00:00:00+00:00",
            upper_bound="2026-08-18T00:00:00+00:00",
        )
        assert prices == [6000]

    def test_stops_bucket_filter_only_applied_when_provided(self, conn):
        insert_observation(conn, _obs(price=5000, stops_bucket="nonstop", observed_at="2026-07-15T02:00:00+00:00"))
        insert_observation(conn, _obs(price=6000, stops_bucket="one_stop", observed_at="2026-07-15T02:00:00+00:00"))

        all_prices = query_comparable_prices(
            conn,
            origin="OSL",
            destination="NRT",
            currency="NOK",
            travel_period_bucket="2027-01",
            trip_length_nights=7,
            duration_tolerance_nights=2,
            stops_bucket=None,
            window_start="2026-07-01T00:00:00+00:00",
            upper_bound="2026-08-18T00:00:00+00:00",
        )
        assert sorted(all_prices) == [5000, 6000]

        nonstop_prices = query_comparable_prices(
            conn,
            origin="OSL",
            destination="NRT",
            currency="NOK",
            travel_period_bucket="2027-01",
            trip_length_nights=7,
            duration_tolerance_nights=2,
            stops_bucket="nonstop",
            window_start="2026-07-01T00:00:00+00:00",
            upper_bound="2026-08-18T00:00:00+00:00",
        )
        assert nonstop_prices == [5000]

    def test_currency_mismatch_is_excluded(self, conn):
        insert_observation(conn, _obs(price=5000, currency="NOK", observed_at="2026-07-15T00:00:00+00:00"))
        insert_observation(conn, _obs(price=40, currency="USD", observed_at="2026-07-15T00:00:00+00:00"))

        prices = query_comparable_prices(
            conn,
            origin="OSL",
            destination="NRT",
            currency="NOK",
            travel_period_bucket="2027-01",
            trip_length_nights=7,
            duration_tolerance_nights=2,
            stops_bucket=None,
            window_start="2026-07-01T00:00:00+00:00",
            upper_bound="2026-08-18T00:00:00+00:00",
        )
        assert prices == [5000]

    def test_null_trip_length_nights_disables_duration_filter(self, conn):
        """Un vol one-way courant (trip_length_nights=None) compare contre tout l'historique
        de la route/periode plutot que d'etre exclu de toute comparaison."""
        insert_observation(conn, _obs(price=5000, trip_length_nights=3, observed_at="2026-07-15T00:00:00+00:00"))
        insert_observation(conn, _obs(price=6000, trip_length_nights=25, observed_at="2026-07-15T00:00:00+00:00"))

        prices = query_comparable_prices(
            conn,
            origin="OSL",
            destination="NRT",
            currency="NOK",
            travel_period_bucket="2027-01",
            trip_length_nights=None,
            duration_tolerance_nights=2,
            stops_bucket=None,
            window_start="2026-07-01T00:00:00+00:00",
            upper_bound="2026-08-18T00:00:00+00:00",
        )
        assert sorted(prices) == [5000, 6000]


class TestNotifiedDealsAndDedup:
    def test_get_last_notification_returns_none_when_absent(self, conn):
        assert get_last_notification(conn, "OSL", "NRT", "2027-01-12", "2027-01-19") is None

    def test_insert_and_retrieve_last_notification(self, conn):
        obs_id = insert_observation(conn, _obs())
        insert_notification(conn, _deal(observation_id=obs_id))

        last = get_last_notification(conn, "OSL", "NRT", "2027-01-12", "2027-01-19")
        assert last is not None
        assert last["price"] == 3200.0
        assert last["score"] == 87

    def test_last_notification_scoped_to_exact_key(self, conn):
        obs_id = insert_observation(conn, _obs())
        insert_notification(conn, _deal(observation_id=obs_id))

        # memes origin/destination, dates differentes -> cle logique differente (spec section 12)
        assert get_last_notification(conn, "OSL", "NRT", "2027-02-01", "2027-02-08") is None

    def test_get_last_notification_returns_most_recent(self, conn):
        obs_id = insert_observation(conn, _obs())
        insert_notification(
            conn, _deal(observation_id=obs_id, notified_at="2026-08-01T00:00:00+00:00", price=3500.0, score=70)
        )
        insert_notification(
            conn, _deal(observation_id=obs_id, notified_at="2026-08-10T00:00:00+00:00", price=3200.0, score=87)
        )

        last = get_last_notification(conn, "OSL", "NRT", "2027-01-12", "2027-01-19")
        assert last["notified_at"] == "2026-08-10T00:00:00+00:00"
        assert last["price"] == 3200.0

    def test_null_return_date_matches_only_null(self, conn):
        obs_id = insert_observation(conn, _obs(return_date=None, trip_length_nights=None))
        insert_notification(conn, _deal(observation_id=obs_id, return_date=None))

        assert get_last_notification(conn, "OSL", "NRT", "2027-01-12", None) is not None
        assert get_last_notification(conn, "OSL", "NRT", "2027-01-12", "2027-01-19") is None


class TestApiUsage:
    def test_get_requests_used_defaults_to_zero(self, conn):
        assert get_requests_used(conn, "2026-08") == 0

    def test_increment_creates_and_accumulates(self, conn):
        increment_requests_used(conn, "2026-08", count=3)
        assert get_requests_used(conn, "2026-08") == 3

        increment_requests_used(conn, "2026-08", count=2)
        assert get_requests_used(conn, "2026-08") == 5

    def test_increment_scoped_per_month(self, conn):
        increment_requests_used(conn, "2026-08", count=3)
        increment_requests_used(conn, "2026-09", count=1)
        assert get_requests_used(conn, "2026-08") == 3
        assert get_requests_used(conn, "2026-09") == 1
