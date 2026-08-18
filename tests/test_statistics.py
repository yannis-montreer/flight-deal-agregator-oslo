"""Tests de flightdeals.analysis.statistics."""
from __future__ import annotations

import pytest

from flightdeals.analysis.statistics import compute_discount, compute_price_stats, percentile_rank


class TestComputePriceStats:
    def test_empty_list_returns_none(self):
        assert compute_price_stats([]) is None

    def test_matches_spec_example_values(self):
        # spec section 9, exemple d'historique
        stats = compute_price_stats([4900, 5200, 5400, 4800, 5100, 5600])
        # trie: [4800, 4900, 5100, 5200, 5400, 5600]
        assert stats.count == 6
        assert stats.minimum == 4800
        assert stats.median == pytest.approx(5150)  # moyenne des 2 valeurs centrales (5100+5200)/2
        assert stats.percentile_25 == pytest.approx(4950)  # interpolation entre 4900 et 5100

    def test_single_value(self):
        stats = compute_price_stats([100.0])
        assert stats.count == 1
        assert stats.median == 100.0
        assert stats.minimum == 100.0
        assert stats.percentile_25 == 100.0

    def test_minimum_is_the_smallest_value_regardless_of_input_order(self):
        stats = compute_price_stats([500, 100, 300])
        assert stats.minimum == 100

    def test_percentile_25_is_below_or_equal_to_median(self):
        stats = compute_price_stats([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000])
        assert stats.percentile_25 <= stats.median


class TestComputeDiscount:
    def test_matches_spec_example(self):
        # spec section 9 : prix actuel 3000, mediane 5000 -> discount 40%
        assert compute_discount(3000, 5000) == pytest.approx(0.4)

    def test_price_above_median_gives_negative_discount(self):
        assert compute_discount(6000, 5000) == pytest.approx(-0.2)

    def test_price_equals_median_gives_zero_discount(self):
        assert compute_discount(5000, 5000) == pytest.approx(0.0)

    def test_zero_or_negative_median_returns_zero_not_exception(self):
        assert compute_discount(100, 0) == 0.0
        assert compute_discount(100, -5) == 0.0


class TestPercentileRank:
    def test_cheapest_price_has_high_rank(self):
        # 3 valeurs sur 4 sont strictement superieures a 100 -> rank 0.75
        assert percentile_rank(100, [100, 200, 300, 400]) == pytest.approx(0.75)

    def test_most_expensive_price_has_rank_zero(self):
        assert percentile_rank(400, [100, 200, 300, 400]) == pytest.approx(0.0)

    def test_empty_history_returns_zero(self):
        assert percentile_rank(100, []) == 0.0

    def test_price_below_all_history_has_rank_one(self):
        assert percentile_rank(50, [100, 200, 300]) == pytest.approx(1.0)
