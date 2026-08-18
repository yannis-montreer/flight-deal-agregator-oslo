"""Statistiques descriptives sur un historique de prix (spec section 9) : mediane, minimum,
percentile, nombre d'observations. Stdlib uniquement (module `statistics`) — les besoins du
MVP (mediane, percentile configurable, min, count) sont entierement couverts, pas besoin de
numpy (voir plan / stack technique : "eviter pandas/numpy si ca n'apporte pas de benefice").
"""
from __future__ import annotations

import statistics as _statistics
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PriceStats:
    count: int
    median: float
    minimum: float
    percentile_25: float


def compute_price_stats(prices: list[float]) -> Optional[PriceStats]:
    """None si prices est vide — l'appelant (scoring.evaluate_deal) traite alors comme
    "historique insuffisant", jamais un crash sur une liste vide."""
    if not prices:
        return None

    sorted_prices = sorted(prices)
    return PriceStats(
        count=len(sorted_prices),
        median=_statistics.median(sorted_prices),
        minimum=sorted_prices[0],
        percentile_25=_percentile(sorted_prices, 0.25),
    )


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Percentile par interpolation lineaire (methode "linear", la plus courante — coherente
    avec numpy.percentile par defaut). Fonctionne pour toute fraction dans [0, 1], pas
    seulement 0.25 : deal.percentile_threshold est configurable (voir config.yaml)."""
    rank = fraction * (len(sorted_values) - 1)
    lower_index = int(rank)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    fractional = rank - lower_index
    return sorted_values[lower_index] + fractional * (sorted_values[upper_index] - sorted_values[lower_index])


def compute_discount(current_price: float, historical_median: float) -> float:
    """discount = 1 - price/mediane (formule exacte du spec section 9). Peut etre negatif si
    le prix courant est plus cher que la mediane — volontairement permis : ce module reste
    une pure fonction de calcul, c'est scoring.py qui filtre sur le seuil minimum_discount."""
    if historical_median <= 0:
        return 0.0  # garde-fou degenere ; ne devrait pas arriver, les prix sont toujours > 0
    return 1 - (current_price / historical_median)


def percentile_rank(current_price: float, historical_prices: list[float]) -> float:
    """Proportion des prix historiques STRICTEMENT superieurs au prix courant, dans [0, 1].
    Composante "a quel point ce prix est rare" du score (spec section 11 : "prix dans les 10%
    les moins chers"). Calcule directement sur la liste deja en memoire, pas une approximation
    depuis les stats resumees (median/p25)."""
    if not historical_prices:
        return 0.0
    higher_count = sum(1 for p in historical_prices if p > current_price)
    return higher_count / len(historical_prices)
