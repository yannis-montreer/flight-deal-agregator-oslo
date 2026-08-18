"""Tests de flightdeals.dedup."""
from __future__ import annotations

from datetime import datetime, timezone

from flightdeals.db.repository import FlightObservation, NotifiedDeal, insert_notification, insert_observation
from flightdeals.dedup import should_notify


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


def _notify(conn, obs_id: int, **overrides) -> None:
    defaults = dict(
        notified_at="2026-08-10T00:00:00+00:00",
        origin="OSL",
        destination="NRT",
        departure_date="2027-01-12",
        return_date="2027-01-19",
        price=3200.0,
        currency="NOK",
        score=87,
        discount=0.4,
        observation_id=obs_id,
    )
    defaults.update(overrides)
    insert_notification(conn, NotifiedDeal(**defaults))


class TestShouldNotify:
    def test_first_notification_for_key_notifies(self, conn):
        decision = should_notify(
            conn,
            origin="OSL",
            destination="NRT",
            departure_date="2027-01-12",
            return_date="2027-01-19",
            current_price=3200.0,
            now=datetime.now(timezone.utc),
            further_drop_threshold=0.10,
            reappear_gap_days=14,
        )
        assert decision.should_notify is True
        assert decision.reason == "first_notification"

    def test_same_price_shortly_after_is_suppressed(self, conn):
        obs_id = insert_observation(conn, _obs())
        _notify(conn, obs_id, notified_at="2026-08-15T00:00:00+00:00", price=3200.0)

        decision = should_notify(
            conn,
            origin="OSL",
            destination="NRT",
            departure_date="2027-01-12",
            return_date="2027-01-19",
            current_price=3200.0,
            now=datetime(2026, 8, 16, tzinfo=timezone.utc),  # 1 jour plus tard
            further_drop_threshold=0.10,
            reappear_gap_days=14,
        )
        assert decision.should_notify is False
        assert decision.reason == "suppressed_duplicate"

    def test_further_drop_of_15_percent_notifies(self, conn):
        obs_id = insert_observation(conn, _obs())
        _notify(conn, obs_id, notified_at="2026-08-15T00:00:00+00:00", price=3200.0)

        # 15% de moins que 3200 = 2720, au-dela du seuil de 10%
        decision = should_notify(
            conn,
            origin="OSL",
            destination="NRT",
            departure_date="2027-01-12",
            return_date="2027-01-19",
            current_price=2720.0,
            now=datetime(2026, 8, 16, tzinfo=timezone.utc),
            further_drop_threshold=0.10,
            reappear_gap_days=14,
        )
        assert decision.should_notify is True
        assert decision.reason == "further_drop"

    def test_further_drop_of_only_5_percent_is_suppressed(self, conn):
        obs_id = insert_observation(conn, _obs())
        _notify(conn, obs_id, notified_at="2026-08-15T00:00:00+00:00", price=3200.0)

        # 5% de moins que 3200 = 3040, sous le seuil de 10%
        decision = should_notify(
            conn,
            origin="OSL",
            destination="NRT",
            departure_date="2027-01-12",
            return_date="2027-01-19",
            current_price=3040.0,
            now=datetime(2026, 8, 16, tzinfo=timezone.utc),
            further_drop_threshold=0.10,
            reappear_gap_days=14,
        )
        assert decision.should_notify is False
        assert decision.reason == "suppressed_duplicate"

    def test_drop_exactly_at_threshold_notifies(self, conn):
        obs_id = insert_observation(conn, _obs())
        _notify(conn, obs_id, notified_at="2026-08-15T00:00:00+00:00", price=3200.0)

        decision = should_notify(
            conn,
            origin="OSL",
            destination="NRT",
            departure_date="2027-01-12",
            return_date="2027-01-19",
            current_price=2880.0,  # exactement 10% de moins que 3200 -> borne inclusive
            now=datetime(2026, 8, 16, tzinfo=timezone.utc),
            further_drop_threshold=0.10,
            reappear_gap_days=14,
        )
        assert decision.should_notify is True
        assert decision.reason == "further_drop"

    def test_gap_of_20_days_notifies_as_reappeared(self, conn):
        obs_id = insert_observation(conn, _obs())
        _notify(conn, obs_id, notified_at="2026-08-01T00:00:00+00:00", price=3200.0)

        decision = should_notify(
            conn,
            origin="OSL",
            destination="NRT",
            departure_date="2027-01-12",
            return_date="2027-01-19",
            current_price=3200.0,  # meme prix, pas une baisse
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),  # 20 jours plus tard
            further_drop_threshold=0.10,
            reappear_gap_days=14,
        )
        assert decision.should_notify is True
        assert decision.reason == "reappeared"

    def test_gap_of_5_days_does_not_reappear(self, conn):
        obs_id = insert_observation(conn, _obs())
        _notify(conn, obs_id, notified_at="2026-08-10T00:00:00+00:00", price=3200.0)

        decision = should_notify(
            conn,
            origin="OSL",
            destination="NRT",
            departure_date="2027-01-12",
            return_date="2027-01-19",
            current_price=3200.0,
            now=datetime(2026, 8, 15, tzinfo=timezone.utc),  # 5 jours plus tard
            further_drop_threshold=0.10,
            reappear_gap_days=14,
        )
        assert decision.should_notify is False
        assert decision.reason == "suppressed_duplicate"

    def test_gap_takes_priority_over_drop_when_both_apply(self, conn):
        obs_id = insert_observation(conn, _obs())
        _notify(conn, obs_id, notified_at="2026-08-01T00:00:00+00:00", price=3200.0)

        decision = should_notify(
            conn,
            origin="OSL",
            destination="NRT",
            departure_date="2027-01-12",
            return_date="2027-01-19",
            current_price=2000.0,  # aussi une grosse baisse...
            now=datetime(2026, 8, 21, tzinfo=timezone.utc),  # ...ET 20 jours d'ecart
            further_drop_threshold=0.10,
            reappear_gap_days=14,
        )
        assert decision.should_notify is True
        assert decision.reason == "reappeared"  # le gap est verifie en premier

    def test_different_departure_date_is_treated_as_new_key(self, conn):
        obs_id = insert_observation(conn, _obs())
        _notify(conn, obs_id, notified_at="2026-08-15T00:00:00+00:00", price=3200.0)

        decision = should_notify(
            conn,
            origin="OSL",
            destination="NRT",
            departure_date="2027-02-01",  # dates differentes -> cle differente
            return_date="2027-02-08",
            current_price=3200.0,
            now=datetime(2026, 8, 16, tzinfo=timezone.utc),
            further_drop_threshold=0.10,
            reappear_gap_days=14,
        )
        assert decision.should_notify is True
        assert decision.reason == "first_notification"

    def test_different_destination_is_treated_as_new_key(self, conn):
        obs_id = insert_observation(conn, _obs())
        _notify(conn, obs_id, notified_at="2026-08-15T00:00:00+00:00", price=3200.0)

        decision = should_notify(
            conn,
            origin="OSL",
            destination="BKK",  # destination differente -> cle differente
            departure_date="2027-01-12",
            return_date="2027-01-19",
            current_price=3200.0,
            now=datetime(2026, 8, 16, tzinfo=timezone.utc),
            further_drop_threshold=0.10,
            reappear_gap_days=14,
        )
        assert decision.should_notify is True
        assert decision.reason == "first_notification"

    def test_one_way_flight_with_null_return_date_matches_correctly(self, conn):
        obs_id = insert_observation(conn, _obs(return_date=None, trip_length_nights=None))
        _notify(conn, obs_id, notified_at="2026-08-15T00:00:00+00:00", price=3200.0, return_date=None)

        decision = should_notify(
            conn,
            origin="OSL",
            destination="NRT",
            departure_date="2027-01-12",
            return_date=None,
            current_price=3200.0,
            now=datetime(2026, 8, 16, tzinfo=timezone.utc),
            further_drop_threshold=0.10,
            reappear_gap_days=14,
        )
        assert decision.should_notify is False  # meme prix, gap court -> suppression normale
