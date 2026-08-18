"""Regle de declenchement d'un deal (spec section 10) et score 0-100 (spec section 11).

Regle de declenchement (toutes les conditions requises) :
    nombre_observations >= minimum_observations
    ET discount >= minimum_discount
    ET prix_actuel <= percentile_25

Score deterministe et configurable (le detail exact du calcul est laisse a l'equipe technique
par le spec ; voir plan pour la justification des poids par defaut) :
    score = 100 * clamp(
        w.discount   * min(1, max(0, discount) / discount_cap)                              +
        w.percentile * percentile_rank(prix_actuel, historique)                              +
        w.directness * directness_bonus[stops_bucket]                                        +
        w.confidence * min(1, (count - minimum_observations) / (saturation - minimum_observations)),
        0, 1)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from flightdeals.analysis.statistics import PriceStats, compute_discount, percentile_rank


@dataclass(frozen=True)
class DealEvaluation:
    """Resultat de l'evaluation d'une observation courante contre son historique.

    stats est None quand l'historique est vide (aucune observation anterieure) — distinct
    d'un historique simplement insuffisant (1-6 observations), ou stats existe mais
    triggers reste False. Dans les deux cas triggers=False et score=0."""

    triggers: bool
    discount: float
    score: int
    stats: Optional[PriceStats]


def evaluate_deal(
    *,
    current_price: float,
    historical_prices: list[float],
    stats: Optional[PriceStats],
    stops_bucket: str,
    minimum_discount: float,
    minimum_observations: int,
    percentile_threshold: float,
    discount_cap: float,
    confidence_saturation_count: int,
    weights: dict,
    directness_bonus: dict,
) -> DealEvaluation:
    """`stats` doit avoir ete calcule par statistics.compute_price_stats sur le MEME
    historical_prices (percentile_25 y est deja calcule avec percentile_threshold=0.25 par
    defaut ; si un percentile_threshold different est configure, l'appelant doit recalculer
    percentile_25 en consequence — voir pipeline.py, jalon M8)."""
    if stats is None or stats.count < minimum_observations:
        # Regle spec section 10 : historique < minimum_observations -> jamais de notification.
        # Court-circuite avant tout calcul de discount/score (rien a comparer, ou pas assez).
        return DealEvaluation(triggers=False, discount=0.0, score=0, stats=stats)

    discount = compute_discount(current_price, stats.median)
    price_at_or_below_percentile = current_price <= stats.percentile_25
    discount_sufficient = discount >= minimum_discount

    triggers = discount_sufficient and price_at_or_below_percentile

    score = _compute_score(
        discount=discount,
        current_price=current_price,
        historical_prices=historical_prices,
        observation_count=stats.count,
        stops_bucket=stops_bucket,
        discount_cap=discount_cap,
        minimum_observations=minimum_observations,
        confidence_saturation_count=confidence_saturation_count,
        weights=weights,
        directness_bonus=directness_bonus,
    )

    return DealEvaluation(triggers=triggers, discount=discount, score=score, stats=stats)


def _compute_score(
    *,
    discount: float,
    current_price: float,
    historical_prices: list[float],
    observation_count: int,
    stops_bucket: str,
    discount_cap: float,
    minimum_observations: int,
    confidence_saturation_count: int,
    weights: dict,
    directness_bonus: dict,
) -> int:
    discount_component = min(1.0, max(0.0, discount) / discount_cap) if discount_cap > 0 else 0.0
    percentile_component = percentile_rank(current_price, historical_prices)
    directness_component = directness_bonus.get(stops_bucket, directness_bonus.get("unknown", 0.5))

    confidence_span = confidence_saturation_count - minimum_observations
    if confidence_span > 0:
        confidence_component = min(1.0, max(0.0, (observation_count - minimum_observations) / confidence_span))
    else:
        # config degeneree (deja rejetee par config.py au chargement, voir
        # confidence_saturation_count > minimum_observations) ; filet de securite ici seulement.
        confidence_component = 1.0

    raw_score = (
        weights.get("discount", 0.0) * discount_component
        + weights.get("percentile", 0.0) * percentile_component
        + weights.get("directness", 0.0) * directness_component
        + weights.get("confidence", 0.0) * confidence_component
    )

    return round(100 * min(1.0, max(0.0, raw_score)))
