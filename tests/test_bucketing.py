"""Tests de flightdeals.analysis.bucketing — fonctions pures, tables de verite."""
from __future__ import annotations

from flightdeals.analysis.bucketing import (
    compute_stops_bucket,
    compute_travel_period_bucket,
    compute_trip_length_nights,
)


class TestComputeTripLengthNights:
    def test_matches_spec_example_seven_nights(self):
        assert compute_trip_length_nights("2027-01-12", "2027-01-19") == 7

    def test_one_way_without_fallback_returns_none(self):
        assert compute_trip_length_nights("2027-01-12", None) is None

    def test_one_way_with_travel_duration_fallback(self):
        assert compute_trip_length_nights("2027-01-12", None, travel_duration_fallback=1) == 2
        assert compute_trip_length_nights("2027-01-12", None, travel_duration_fallback=2) == 7
        assert compute_trip_length_nights("2027-01-12", None, travel_duration_fallback=3) == 14

    def test_missing_departure_date_returns_none(self):
        assert compute_trip_length_nights(None, "2027-01-19") is None

    def test_malformed_date_returns_none_not_exception(self):
        assert compute_trip_length_nights("not-a-date", "2027-01-19") is None
        assert compute_trip_length_nights("2027-01-12", "also-not-a-date") is None

    def test_return_before_departure_returns_none(self):
        # donnee aberrante ; ne doit pas produire un nombre de nuits negatif silencieusement
        assert compute_trip_length_nights("2027-01-19", "2027-01-12") is None

    def test_same_day_return_is_zero_nights(self):
        assert compute_trip_length_nights("2027-01-12", "2027-01-12") == 0


class TestComputeTravelPeriodBucket:
    def test_truncates_to_year_month(self):
        assert compute_travel_period_bucket("2027-01-12") == "2027-01"

    def test_missing_date_returns_none(self):
        assert compute_travel_period_bucket(None) is None

    def test_malformed_date_returns_none(self):
        assert compute_travel_period_bucket("not-a-date") is None


class TestComputeStopsBucket:
    def test_zero_stops_is_nonstop(self):
        assert compute_stops_bucket(0) == "nonstop"

    def test_one_stop(self):
        assert compute_stops_bucket(1) == "one_stop"

    def test_two_or_more_is_multi_stop(self):
        assert compute_stops_bucket(2) == "multi_stop"
        assert compute_stops_bucket(5) == "multi_stop"

    def test_none_is_unknown(self):
        assert compute_stops_bucket(None) == "unknown"
