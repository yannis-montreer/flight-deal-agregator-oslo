"""Construit les requetes google_travel_explore (mode principal, budget-efficace, confirme
par le Spike) pour OSL et parse les destinations[] renvoyees en observations brutes.

google_flights (mode point-a-point) a ete valide dans le Spike mais n'est deliberement PAS
utilise en collecte quotidienne : 1 requete par destination serait bien plus couteux qu'1
requete pour ~80 destinations via explore (voir plan et spike/SPIKE_NOTES.md).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from flightdeals.collectors.serpapi_client import QuotaExceededError, SerpApiClient, SerpApiError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawObservation:
    """Une destination parsee depuis une reponse google_travel_explore, AVANT bucketing
    (flightdeals.analysis.bucketing) et avant insertion en base (flightdeals.db.repository).
    """

    origin: str
    destination: Optional[str]  # code IATA ; None si absent de la reponse
    destination_name: Optional[str]
    departure_date: Optional[str]
    return_date: Optional[str]
    price: Optional[float]
    currency: str
    airline: Optional[str]
    stops: Optional[int]
    duration_minutes: Optional[int]
    source: str
    source_url: Optional[str]

    @property
    def is_exploitable(self) -> bool:
        """Une entree sans prix n'est pas exploitable pour la detection de deal — frequent sur
        le bucket ~2 semaines (10/84 seulement lors du Spike, voir spike/SPIKE_NOTES.md), ce
        n'est pas une erreur de parsing, juste une absence de cache prix cote Google."""
        return self.price is not None and self.destination is not None and self.departure_date is not None


def _parse_explore_entry(entry: dict, *, origin: str, currency: str) -> RawObservation:
    """Parsing defensif : .get() partout, jamais d'indexation directe. Le format SerpApi n'est
    pas un contrat stable (scrape d'une UI Google), voir risques du plan."""
    airport = entry.get("destination_airport")
    airport_code = airport.get("code") if isinstance(airport, dict) else None

    return RawObservation(
        origin=origin,
        destination=airport_code,
        destination_name=entry.get("name"),
        departure_date=entry.get("start_date"),
        return_date=entry.get("end_date"),
        price=entry.get("flight_price"),
        currency=currency,
        airline=entry.get("airline"),
        stops=entry.get("number_of_stops"),
        duration_minutes=entry.get("flight_duration"),
        source="serpapi_google_travel_explore",
        source_url=entry.get("link"),
    )


def fetch_explore_destinations(
    client: SerpApiClient,
    *,
    origin: str,
    currency: str,
    travel_duration: int,
    arrival_area_id: Optional[str] = None,
) -> list[RawObservation]:
    """Un appel SerpApi google_travel_explore -> une RawObservation par destination retournee
    (exploitable ou non, voir RawObservation.is_exploitable). Une erreur de parsing sur UNE
    entree est loguee et ignoree, jamais fatale aux autres entrees de la meme reponse."""
    params: dict = {
        "engine": "google_travel_explore",
        "departure_id": origin,
        "travel_duration": travel_duration,
        "currency": currency,
    }
    if arrival_area_id:
        params["arrival_area_id"] = arrival_area_id

    data = client.search(params)
    raw_destinations = data.get("destinations", [])

    observations: list[RawObservation] = []
    for entry in raw_destinations:
        try:
            observations.append(_parse_explore_entry(entry, origin=origin, currency=currency))
        except Exception:
            logger.exception("Echec de parsing d'une destination explore (ignoree): %r", entry)
            continue

    exploitable_count = sum(1 for o in observations if o.is_exploitable)
    logger.info(
        "explore travel_duration=%d: %d destinations, %d exploitables",
        travel_duration, len(observations), exploitable_count,
    )
    return observations


def fetch_all_explore_destinations(
    client: SerpApiClient,
    *,
    origin: str,
    currency: str,
    travel_durations: "tuple[int, ...] | list[int]",
    arrival_area_id: Optional[str] = None,
) -> list[RawObservation]:
    """Enchaine les N requetes explore (une par duree configuree) — fonction appelee par
    pipeline.run_once() (jalon M8). QuotaExceededError PROPAGE volontairement (pas attrapee
    ici) pour que pipeline.py arrete tous les appels SerpApi restants du jour ; toute autre
    erreur sur une duree donnee est loguee et n'empeche pas les durees suivantes."""
    all_observations: list[RawObservation] = []
    for duration in travel_durations:
        try:
            all_observations.extend(
                fetch_explore_destinations(
                    client,
                    origin=origin,
                    currency=currency,
                    travel_duration=duration,
                    arrival_area_id=arrival_area_id,
                )
            )
        except QuotaExceededError:
            raise
        except SerpApiError:
            logger.exception(
                "Echec de la collecte pour travel_duration=%d (ignore, les autres durees continuent)",
                duration,
            )
            continue
    return all_observations
