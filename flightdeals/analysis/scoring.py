"""Regle de declenchement d'un deal (spec section 10, etendue) et score 0-100 (spec section 11).

Regle de declenchement (TOUTES les conditions requises) :
    nombre_observations >= minimum_observations
    ET discount >= minimum_discount
    ET prix_actuel <= percentile_25
    ET min_trip_length_nights <= duree_du_sejour <= max_trip_length_nights
       (extension demandee par l'utilisateur : exclut les sejours trop courts type weekend
       et trop longs type 1 mois, meme si le prix est excellent — voir _check_trip_length)
    ET duree_vol pas anormalement longue vs la moyenne historique de cette destination
       (extension demandee par l'utilisateur post-MVP : evite les deals bon marche mais avec
       une escale a rallonge ; voir _check_duration ci-dessous pour les garde-fous)

Score deterministe et configurable (le detail exact du calcul est laisse a l'equipe technique
par le spec ; voir plan pour la justification des poids par defaut) :
    score = 100 * clamp(
        w.discount   * min(1, max(0, discount) / discount_cap)                              +
        w.percentile * percentile_rank(prix_actuel, historique)                              +
        w.directness * directness_bonus[stops_bucket]                                        +
        w.confidence * min(1, (count - minimum_observations) / (saturation - minimum_observations)),
        0, 1)

Le filtre de duree n'entre PAS dans le score (qui reste celui du spec) : il agit uniquement
sur `triggers`, comme une condition d'exclusion supplementaire — un itineraire avec une bonne
note peut donc quand meme etre exclu s'il a une duree aberrante.
"""
from __future__ import annotations

import statistics as _statistics
from dataclasses import dataclass
from typing import Optional

from flightdeals.analysis.statistics import PriceStats, compute_discount, percentile_rank


@dataclass(frozen=True)
class DealEvaluation:
    """Resultat de l'evaluation d'une observation courante contre son historique.

    stats est None quand l'historique est vide (aucune observation anterieure) — distinct
    d'un historique simplement insuffisant (1-6 observations), ou stats existe mais
    triggers reste False. Dans les deux cas triggers=False et score=0.

    exclusion_reason est None quand triggers=True, sinon la PREMIERE condition qui a echoue
    parmi : "insufficient_history" | "discount_below_threshold" | "price_above_percentile" |
    "trip_length_out_of_range" | "duration_deviation" — utile pour les logs/debug
    ("pourquoi ce deal n'a pas notifie ?")."""

    triggers: bool
    discount: float
    score: int
    stats: Optional[PriceStats]
    exclusion_reason: Optional[str] = None


def evaluate_deal(
    *,
    current_price: float,
    historical_prices: list[float],
    stats: Optional[PriceStats],
    stops_bucket: str,
    trip_length_nights: Optional[int],
    current_duration_minutes: Optional[int],
    historical_durations: list[int],
    minimum_discount: float,
    minimum_observations: int,
    percentile_threshold: float,
    discount_cap: float,
    confidence_saturation_count: int,
    max_duration_deviation_ratio: float,
    min_trip_length_nights: int,
    max_trip_length_nights: int,
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
        return DealEvaluation(triggers=False, discount=0.0, score=0, stats=stats, exclusion_reason="insufficient_history")

    discount = compute_discount(current_price, stats.median)
    price_at_or_below_percentile = current_price <= stats.percentile_25
    discount_sufficient = discount >= minimum_discount
    trip_length_ok = check_trip_length(trip_length_nights, min_trip_length_nights, max_trip_length_nights)
    duration_ok = _check_duration(
        current_duration_minutes, historical_durations, minimum_observations, max_duration_deviation_ratio
    )

    triggers = discount_sufficient and price_at_or_below_percentile and trip_length_ok and duration_ok

    # Le score est TOUJOURS calcule, meme si triggers=False (utile pour les logs/debug —
    # voir plan, risque "cold start" : visibilite sur les deals proches du seuil).
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

    exclusion_reason = None
    if not triggers:
        if not discount_sufficient:
            exclusion_reason = "discount_below_threshold"
        elif not price_at_or_below_percentile:
            exclusion_reason = "price_above_percentile"
        elif not trip_length_ok:
            exclusion_reason = "trip_length_out_of_range"
        else:
            exclusion_reason = "duration_deviation"

    return DealEvaluation(triggers=triggers, discount=discount, score=score, stats=stats, exclusion_reason=exclusion_reason)


def check_trip_length(
    trip_length_nights: Optional[int], min_nights: int, max_nights: int
) -> bool:
    """True = duree de sejour dans la fourchette voulue (bornes inclusives), False = exclue.

    Contrairement a _check_duration (vol) qui est fail-open sur donnee manquante, ici
    trip_length_nights=None (vol one-way, pas de date de retour) est traite comme HORS
    fourchette : la demande porte explicitement sur "la duree de A/R", donc un aller simple
    ne correspond a rien de mesurable dans ce filtre — pas de round-trip, pas de duree a
    juger, exclu par construction plutot que laisse passer par defaut.

    Publique (pas de prefixe _) : reutilisee par pipeline._check_cold_start_signals pour
    appliquer le meme filtre de duree de sejour aux candidats "historique insuffisant"
    (voir plan cold-start / signal Google, sinon on alerterait sur des sejours hors des
    bornes voulues juste parce qu'ils n'ont pas encore d'historique)."""
    if trip_length_nights is None:
        return False
    return min_nights <= trip_length_nights <= max_nights


def _check_duration(
    current_duration_minutes: Optional[int],
    historical_durations: list[int],
    minimum_observations: int,
    max_duration_deviation_ratio: float,
) -> bool:
    """True = duree acceptable (ou impossible a juger -> ne bloque pas), False = exclue.

    Deux garde-fous "fail-open" deliberes (coherents avec le reste du projet : une donnee
    manquante ne doit jamais bloquer un vrai deal) :
    - current_duration_minutes absent (API ne l'a pas fourni) -> pas de blocage.
    - historique de duree encore trop court pour juger de ce qui est "normal" sur cette
      destination -> pas de blocage (le seuil se durcit naturellement une fois assez
      d'observations accumulees, memes minimum_observations que pour le reste)."""
    if current_duration_minutes is None:
        return True
    if len(historical_durations) < minimum_observations:
        return True

    average = _statistics.mean(historical_durations)
    if average <= 0:
        return True

    return current_duration_minutes <= average * (1 + max_duration_deviation_ratio)


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
