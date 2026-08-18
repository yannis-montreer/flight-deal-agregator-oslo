"""Tests de flightdeals.analysis.scoring."""
from __future__ import annotations

import pytest

from flightdeals.analysis.scoring import evaluate_deal
from flightdeals.analysis.statistics import compute_price_stats

WEIGHTS = {"discount": 0.45, "percentile": 0.25, "directness": 0.10, "confidence": 0.20}
DIRECTNESS_BONUS = {"nonstop": 1.0, "one_stop": 0.5, "multi_stop": 0.0, "unknown": 0.5}

COMMON_KWARGS = dict(
    minimum_discount=0.30,
    minimum_observations=7,
    percentile_threshold=0.25,
    discount_cap=0.60,
    confidence_saturation_count=30,
    weights=WEIGHTS,
    directness_bonus=DIRECTNESS_BONUS,
)


def _history(prices):
    return prices, compute_price_stats(prices)


class TestTriggerConditions:
    def test_empty_history_never_triggers(self):
        result = evaluate_deal(
            current_price=3000, historical_prices=[], stats=None, stops_bucket="nonstop", **COMMON_KWARGS
        )
        assert result.triggers is False
        assert result.score == 0

    def test_fewer_than_minimum_observations_never_triggers_even_with_huge_discount(self):
        prices, stats = _history([5000, 5100, 5200, 5300, 5400, 5500])  # 6 < 7 minimum
        result = evaluate_deal(
            current_price=1000, historical_prices=prices, stats=stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        assert result.triggers is False

    def test_insufficient_discount_does_not_trigger(self):
        prices, stats = _history([5000] * 7)
        result = evaluate_deal(
            current_price=4900,  # ~2% de discount seulement
            historical_prices=prices,
            stats=stats,
            stops_bucket="nonstop",
            **COMMON_KWARGS,
        )
        assert result.triggers is False

    def test_discount_sufficient_but_price_above_percentile_25_does_not_trigger(self):
        # trie: [1000, 1000, 1500, 2000, 2500, 3000, 10000] -> mediane=2000, p25=1250
        prices, stats = _history([1000, 1000, 1500, 2000, 2500, 3000, 10000])
        assert stats.median == pytest.approx(2000)
        assert stats.percentile_25 == pytest.approx(1250)

        # current=1300 -> discount = 1-1300/2000 = 0.35 (>=0.30 ok) MAIS 1300 > p25=1250
        result = evaluate_deal(
            current_price=1300, historical_prices=prices, stats=stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        assert result.discount >= 0.30
        assert result.triggers is False

    def test_all_three_conditions_met_triggers(self):
        prices, stats = _history([4900, 5200, 5400, 4800, 5100, 5600, 5000])  # 7 obs
        result = evaluate_deal(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        assert result.triggers is True
        assert result.discount > 0.30

    def test_boundary_exactly_at_thresholds_triggers(self):
        # 10 observations identiques -> mediane = p25 = 1000
        prices, stats = _history([1000] * 10)
        # discount exactement 30% : current = 700
        result = evaluate_deal(
            current_price=700, historical_prices=prices, stats=stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        assert result.discount == pytest.approx(0.30)
        assert result.triggers is True  # bornes inclusives (>=, <=), conforme au spec


class TestScoreFormula:
    def test_score_is_always_between_0_and_100(self):
        prices, stats = _history([4900, 5200, 5400, 4800, 5100, 5600, 5000])
        result = evaluate_deal(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        assert 0 <= result.score <= 100

    def test_no_trigger_still_yields_a_score(self):
        # meme un deal qui ne se declenche pas a un score calcule (utile pour debug/logs),
        # juste pas de notification envoyee (decidee ailleurs, par le champ triggers)
        prices, stats = _history([5000] * 7)
        result = evaluate_deal(
            current_price=4900, historical_prices=prices, stats=stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        assert result.triggers is False
        assert result.score > 0

    def test_bigger_discount_yields_higher_score_all_else_equal(self):
        prices, stats = _history([5000] * 10)
        smaller = evaluate_deal(
            current_price=3400, historical_prices=prices, stats=stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        bigger = evaluate_deal(
            current_price=2000, historical_prices=prices, stats=stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        assert bigger.score > smaller.score

    def test_nonstop_scores_higher_than_multistop_all_else_equal(self):
        prices, stats = _history([5000] * 10)
        nonstop = evaluate_deal(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        multistop = evaluate_deal(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="multi_stop", **COMMON_KWARGS
        )
        assert nonstop.score > multistop.score

    def test_more_observations_yields_higher_confidence_all_else_equal(self):
        few_prices, few_stats = _history([5000] * 7)  # pile au minimum -> confiance nulle
        many_prices, many_stats = _history([5000] * 30)  # au seuil de saturation -> confiance max
        few = evaluate_deal(
            current_price=3000, historical_prices=few_prices, stats=few_stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        many = evaluate_deal(
            current_price=3000, historical_prices=many_prices, stats=many_stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        assert many.score > few.score

    def test_unknown_stops_bucket_gets_neutral_bonus_between_multistop_and_nonstop(self):
        prices, stats = _history([5000] * 10)
        unknown = evaluate_deal(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="unknown", **COMMON_KWARGS
        )
        multistop = evaluate_deal(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="multi_stop", **COMMON_KWARGS
        )
        nonstop = evaluate_deal(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        assert multistop.score < unknown.score < nonstop.score

    def test_worked_example_marginal_deal_lands_mid_range(self):
        # deal pile au seuil (30% discount, nonstop, count=10) -> score ni tres bas ni tres haut
        prices, stats = _history([1000] * 10)
        result = evaluate_deal(
            current_price=700, historical_prices=prices, stats=stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        assert 40 <= result.score <= 75

    def test_worked_example_blowout_deal_lands_high(self):
        # 55% de discount, nonstop, count=40 (confiance saturee) -> score eleve
        prices, stats = _history([5000] * 40)
        result = evaluate_deal(
            current_price=2250, historical_prices=prices, stats=stats, stops_bucket="nonstop", **COMMON_KWARGS
        )
        assert result.score >= 85
