"""Calcul des champs de normalisation/comparaison (spec section 8) : duree de sejour,
periode de voyage, palier d'escales. Fonctions pures, aucune dependance a la DB ni a l'API.

Deux notions distinctes (voir plan) :
- Buckets de REQUETE (quelle duree demander a SerpApi) : deja fournis par l'enum
  travel_duration (1/2/3) de google_travel_explore, ce module ne s'en occupe pas.
- Matching de COMPARAISON (ce module) : valeurs calculees et stockees par ligne dans
  flight_observations, comparees ensuite avec une fenetre de tolerance glissante (pas des
  categories figees) par flightdeals.db.repository.query_comparable_prices.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

# Filet de securite si end_date manque sur une entree isolee. Le Spike confirme que end_date
# est normalement present dans les reponses google_travel_explore (voir spike/SPIKE_NOTES.md)
# — ce mapping n'est donc plus le chemin principal attendu, juste une garde defensive.
_NIGHTS_BY_TRAVEL_DURATION = {1: 2, 2: 7, 3: 14}


def compute_trip_length_nights(
    departure_date: Optional[str],
    return_date: Optional[str],
    *,
    travel_duration_fallback: Optional[int] = None,
) -> Optional[int]:
    """Nombre de nuits du sejour, ou None si non calculable (one-way sans fallback, dates
    manquantes/invalides, ou return_date anterieur a departure_date)."""
    if not departure_date:
        return None
    if not return_date:
        if travel_duration_fallback is not None:
            return _NIGHTS_BY_TRAVEL_DURATION.get(travel_duration_fallback)
        return None

    try:
        dep = date.fromisoformat(departure_date)
        ret = date.fromisoformat(return_date)
    except ValueError:
        return None

    nights = (ret - dep).days
    return nights if nights >= 0 else None


def compute_travel_period_bucket(departure_date: Optional[str]) -> Optional[str]:
    """'YYYY-MM' derive de departure_date. None si manquant/invalide — l'appelant doit alors
    traiter l'observation comme non-bucketable (voir RawObservation.is_exploitable)."""
    if not departure_date:
        return None
    try:
        d = date.fromisoformat(departure_date)
    except ValueError:
        return None
    return f"{d.year:04d}-{d.month:02d}"


def compute_stops_bucket(stops: Optional[int]) -> str:
    """'nonstop' | 'one_stop' | 'multi_stop' | 'unknown' (donnee absente de l'API)."""
    if stops is None:
        return "unknown"
    if stops == 0:
        return "nonstop"
    if stops == 1:
        return "one_stop"
    return "multi_stop"
