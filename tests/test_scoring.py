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
    max_duration_deviation_ratio=0.5,
    # Fourchette large par defaut : les tests qui ne testent pas specifiquement le filtre de
    # duree de sejour n'ont pas a s'en soucier (voir TestTripLengthFilter pour les tests dedies).
    min_trip_length_nights=0,
    max_trip_length_nights=9999,
    weights=WEIGHTS,
    directness_bonus=DIRECTNESS_BONUS,
)


def _history(prices):
    return prices, compute_price_stats(prices)


def _evaluate(*, trip_length_nights=10, current_duration_minutes=None, historical_durations=None, **kwargs):
    """Enveloppe evaluate_deal avec les kwargs communs + des valeurs par defaut "aucune
    donnee de duree" / "duree de sejour neutre (10 nuits, dans la fourchette large par
    defaut)" pour les tests qui ne testent pas specifiquement ces filtres (fail-open par
    design : pas de duree de vol -> ne bloque jamais, voir TestDurationExclusion). `kwargs`
    peut surcharger n'importe quelle valeur de COMMON_KWARGS (ex: min/max_trip_length_nights)."""
    merged = {**COMMON_KWARGS, **kwargs}
    return evaluate_deal(
        trip_length_nights=trip_length_nights,
        current_duration_minutes=current_duration_minutes,
        historical_durations=historical_durations or [],
        **merged,
    )


class TestTriggerConditions:
    def test_empty_history_never_triggers(self):
        result = _evaluate(current_price=3000, historical_prices=[], stats=None, stops_bucket="nonstop")
        assert result.triggers is False
        assert result.score == 0
        assert result.exclusion_reason == "insufficient_history"

    def test_fewer_than_minimum_observations_never_triggers_even_with_huge_discount(self):
        prices, stats = _history([5000, 5100, 5200, 5300, 5400, 5500])  # 6 < 7 minimum
        result = _evaluate(current_price=1000, historical_prices=prices, stats=stats, stops_bucket="nonstop")
        assert result.triggers is False
        assert result.exclusion_reason == "insufficient_history"

    def test_insufficient_discount_does_not_trigger(self):
        prices, stats = _history([5000] * 7)
        result = _evaluate(
            current_price=4900,  # ~2% de discount seulement
            historical_prices=prices,
            stats=stats,
            stops_bucket="nonstop",
        )
        assert result.triggers is False
        assert result.exclusion_reason == "discount_below_threshold"

    def test_discount_sufficient_but_price_above_percentile_25_does_not_trigger(self):
        # trie: [1000, 1000, 1500, 2000, 2500, 3000, 10000] -> mediane=2000, p25=1250
        prices, stats = _history([1000, 1000, 1500, 2000, 2500, 3000, 10000])
        assert stats.median == pytest.approx(2000)
        assert stats.percentile_25 == pytest.approx(1250)

        # current=1300 -> discount = 1-1300/2000 = 0.35 (>=0.30 ok) MAIS 1300 > p25=1250
        result = _evaluate(current_price=1300, historical_prices=prices, stats=stats, stops_bucket="nonstop")
        assert result.discount >= 0.30
        assert result.triggers is False
        assert result.exclusion_reason == "price_above_percentile"

    def test_all_conditions_met_triggers(self):
        prices, stats = _history([4900, 5200, 5400, 4800, 5100, 5600, 5000])  # 7 obs
        result = _evaluate(current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop")
        assert result.triggers is True
        assert result.discount > 0.30
        assert result.exclusion_reason is None

    def test_boundary_exactly_at_thresholds_triggers(self):
        # 10 observations identiques -> mediane = p25 = 1000
        prices, stats = _history([1000] * 10)
        # discount exactement 30% : current = 700
        result = _evaluate(current_price=700, historical_prices=prices, stats=stats, stops_bucket="nonstop")
        assert result.discount == pytest.approx(0.30)
        assert result.triggers is True  # bornes inclusives (>=, <=), conforme au spec


class TestTripLengthFilter:
    """min_trip_length_nights=6, max_trip_length_nights=14 (valeurs demandees par
    l'utilisateur) passes explicitement ici plutot que via le defaut large de COMMON_KWARGS."""

    def test_within_range_triggers(self):
        prices, stats = _history([5000] * 7)
        result = _evaluate(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop",
            trip_length_nights=10, min_trip_length_nights=6, max_trip_length_nights=14,
        )
        assert result.triggers is True

    def test_too_short_excludes_even_with_great_price(self):
        prices, stats = _history([5000] * 7)
        result = _evaluate(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop",
            trip_length_nights=2,  # weekend, sous le minimum de 6
            min_trip_length_nights=6, max_trip_length_nights=14,
        )
        assert result.triggers is False
        assert result.exclusion_reason == "trip_length_out_of_range"
        assert result.score > 0  # le score reste calcule malgre l'exclusion

    def test_too_long_excludes_even_with_great_price(self):
        prices, stats = _history([5000] * 7)
        result = _evaluate(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop",
            trip_length_nights=30,  # ~1 mois, au-dela du maximum de 14
            min_trip_length_nights=6, max_trip_length_nights=14,
        )
        assert result.triggers is False
        assert result.exclusion_reason == "trip_length_out_of_range"

    def test_exact_boundaries_are_inclusive(self):
        prices, stats = _history([5000] * 7)
        low = _evaluate(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop",
            trip_length_nights=6, min_trip_length_nights=6, max_trip_length_nights=14,
        )
        high = _evaluate(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop",
            trip_length_nights=14, min_trip_length_nights=6, max_trip_length_nights=14,
        )
        assert low.triggers is True
        assert high.triggers is True

    def test_none_trip_length_is_excluded_not_allowed_through(self):
        # contrairement au filtre de duree de vol (fail-open), l'absence de duree de sejour
        # (vol one-way, pas de date de retour) est traitee comme HORS fourchette : la
        # demande porte explicitement sur "la duree de A/R"
        prices, stats = _history([5000] * 7)
        result = _evaluate(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop",
            trip_length_nights=None, min_trip_length_nights=6, max_trip_length_nights=14,
        )
        assert result.triggers is False
        assert result.exclusion_reason == "trip_length_out_of_range"


class TestDurationExclusion:
    def test_no_current_duration_never_blocks(self):
        prices, stats = _history([5000] * 7)
        result = _evaluate(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop",
            current_duration_minutes=None, historical_durations=[400] * 7,
        )
        assert result.triggers is True

    def test_insufficient_duration_history_never_blocks(self):
        prices, stats = _history([5000] * 7)
        result = _evaluate(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop",
            current_duration_minutes=2000,  # enorme...
            historical_durations=[400, 410, 420],  # ...mais seulement 3 < minimum_observations=7
        )
        assert result.triggers is True

    def test_duration_within_deviation_ratio_triggers(self):
        prices, stats = _history([5000] * 7)
        # moyenne historique = 400min, ratio 0.5 -> seuil = 600min
        result = _evaluate(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop",
            current_duration_minutes=590, historical_durations=[400] * 7,
        )
        assert result.triggers is True

    def test_duration_exactly_at_threshold_triggers(self):
        prices, stats = _history([5000] * 7)
        # moyenne=400, ratio=0.5 -> seuil exact = 600
        result = _evaluate(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop",
            current_duration_minutes=600, historical_durations=[400] * 7,
        )
        assert result.triggers is True  # borne inclusive

    def test_duration_beyond_deviation_ratio_excludes_even_with_great_price(self):
        prices, stats = _history([5000] * 7)
        result = _evaluate(
            current_price=3000,  # excellent prix, remplirait toutes les autres conditions
            historical_prices=prices, stats=stats, stops_bucket="nonstop",
            current_duration_minutes=650,  # > 400*1.5=600
            historical_durations=[400] * 7,
        )
        assert result.triggers is False
        assert result.exclusion_reason == "duration_deviation"
        assert result.score > 0  # le score reste calcule malgre l'exclusion (utile pour debug)

    def test_zero_average_historical_duration_never_blocks(self):
        prices, stats = _history([5000] * 7)
        result = _evaluate(
            current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop",
            current_duration_minutes=100, historical_durations=[0] * 7,  # garde-fou degenere
        )
        assert result.triggers is True


class TestScoreFormula:
    def test_score_is_always_between_0_and_100(self):
        prices, stats = _history([4900, 5200, 5400, 4800, 5100, 5600, 5000])
        result = _evaluate(current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop")
        assert 0 <= result.score <= 100

    def test_no_trigger_still_yields_a_score(self):
        # meme un deal qui ne se declenche pas a un score calcule (utile pour debug/logs),
        # juste pas de notification envoyee (decidee ailleurs, par le champ triggers)
        prices, stats = _history([5000] * 7)
        result = _evaluate(current_price=4900, historical_prices=prices, stats=stats, stops_bucket="nonstop")
        assert result.triggers is False
        assert result.score > 0

    def test_bigger_discount_yields_higher_score_all_else_equal(self):
        prices, stats = _history([5000] * 10)
        smaller = _evaluate(current_price=3400, historical_prices=prices, stats=stats, stops_bucket="nonstop")
        bigger = _evaluate(current_price=2000, historical_prices=prices, stats=stats, stops_bucket="nonstop")
        assert bigger.score > smaller.score

    def test_nonstop_scores_higher_than_multistop_all_else_equal(self):
        prices, stats = _history([5000] * 10)
        nonstop = _evaluate(current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop")
        multistop = _evaluate(current_price=3000, historical_prices=prices, stats=stats, stops_bucket="multi_stop")
        assert nonstop.score > multistop.score

    def test_more_observations_yields_higher_confidence_all_else_equal(self):
        few_prices, few_stats = _history([5000] * 7)  # pile au minimum -> confiance nulle
        many_prices, many_stats = _history([5000] * 30)  # au seuil de saturation -> confiance max
        few = _evaluate(current_price=3000, historical_prices=few_prices, stats=few_stats, stops_bucket="nonstop")
        many = _evaluate(current_price=3000, historical_prices=many_prices, stats=many_stats, stops_bucket="nonstop")
        assert many.score > few.score

    def test_unknown_stops_bucket_gets_neutral_bonus_between_multistop_and_nonstop(self):
        prices, stats = _history([5000] * 10)
        unknown = _evaluate(current_price=3000, historical_prices=prices, stats=stats, stops_bucket="unknown")
        multistop = _evaluate(current_price=3000, historical_prices=prices, stats=stats, stops_bucket="multi_stop")
        nonstop = _evaluate(current_price=3000, historical_prices=prices, stats=stats, stops_bucket="nonstop")
        assert multistop.score < unknown.score < nonstop.score

    def test_worked_example_marginal_deal_lands_mid_range(self):
        # deal pile au seuil (30% discount, nonstop, count=10) -> score ni tres bas ni tres haut
        prices, stats = _history([1000] * 10)
        result = _evaluate(current_price=700, historical_prices=prices, stats=stats, stops_bucket="nonstop")
        assert 40 <= result.score <= 75

    def test_worked_example_blowout_deal_lands_high(self):
        # 55% de discount, nonstop, count=40 (confiance saturee) -> score eleve
        prices, stats = _history([5000] * 40)
        result = _evaluate(current_price=2250, historical_prices=prices, stats=stats, stops_bucket="nonstop")
        assert result.score >= 85
