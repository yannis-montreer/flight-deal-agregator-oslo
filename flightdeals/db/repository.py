"""Couche d'acces aux donnees : tout le SQL du projet vit ici, aucun autre module ne doit
construire de requete. Les autres modules manipulent des dataclasses (FlightObservation,
NotifiedDeal) et appellent ces fonctions.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class FlightObservation:
    """Une ligne a inserer dans flight_observations. Les 3 derniers champs (trip_length_nights,
    travel_period_bucket, stops_bucket) sont calcules par flightdeals.analysis.bucketing
    (jalon M4) avant l'insertion — repository.py ne fait aucun calcul, juste du stockage."""

    observed_at: str
    origin: str
    destination: str
    destination_name: Optional[str]
    departure_date: str
    return_date: Optional[str]
    price: float
    currency: str
    airline: Optional[str]
    stops: Optional[int]
    duration_minutes: Optional[int]
    source: str
    source_url: Optional[str]
    trip_length_nights: Optional[int]
    travel_period_bucket: str
    stops_bucket: str


@dataclass(frozen=True)
class NotifiedDeal:
    """Une ligne a inserer dans notified_deals — uniquement apres un envoi Telegram confirme
    (voir flightdeals.notify.telegram, jalon M7)."""

    notified_at: str
    origin: str
    destination: str
    departure_date: str
    return_date: Optional[str]
    price: float
    currency: str
    score: int
    discount: float
    observation_id: int


def insert_observation(conn: sqlite3.Connection, obs: FlightObservation) -> int:
    cursor = conn.execute(
        """
        INSERT INTO flight_observations (
            observed_at, origin, destination, destination_name, departure_date, return_date,
            price, currency, airline, stops, duration_minutes, source, source_url,
            trip_length_nights, travel_period_bucket, stops_bucket
        ) VALUES (
            :observed_at, :origin, :destination, :destination_name, :departure_date, :return_date,
            :price, :currency, :airline, :stops, :duration_minutes, :source, :source_url,
            :trip_length_nights, :travel_period_bucket, :stops_bucket
        )
        """,
        asdict(obs),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def query_comparable_prices(
    conn: sqlite3.Connection,
    *,
    origin: str,
    destination: str,
    currency: str,
    travel_period_bucket: str,
    trip_length_nights: Optional[int],
    duration_tolerance_nights: int,
    stops_bucket: Optional[str],
    window_start: str,
    upper_bound: str,
) -> list[float]:
    """Prix historiques "comparables" a une observation courante (voir spec section 8 :
    normalisation par route/duree/periode/escales), tries croissant.

    IMPORTANT anti-auto-comparaison : `upper_bound` doit etre le timestamp de DEBUT du run en
    cours (capture une seule fois dans pipeline.run_once, jamais recalcule par destination) —
    sinon une observation inseree plus tot dans le MEME run pollue la comparaison d'une
    destination traitee plus tard dans ce meme run. Voir schema.sql / plan pour le detail.

    trip_length_nights=None (vol one-way) desactive le filtre de duree plutot que d'exclure
    tout : comparer entre one-way est encore mieux que ne pas comparer du tout.
    stops_bucket=None desactive le filtre d'escales (implemente le "quand disponible" du spec).
    """
    params: dict = {
        "origin": origin,
        "destination": destination,
        "currency": currency,
        "travel_period_bucket": travel_period_bucket,
        "window_start": window_start,
        "upper_bound": upper_bound,
    }

    sql = """
        SELECT price FROM flight_observations
        WHERE origin = :origin
          AND destination = :destination
          AND currency = :currency
          AND travel_period_bucket = :travel_period_bucket
          AND observed_at >= :window_start AND observed_at < :upper_bound
    """

    if trip_length_nights is not None:
        params["nights_min"] = trip_length_nights - duration_tolerance_nights
        params["nights_max"] = trip_length_nights + duration_tolerance_nights
        sql += " AND trip_length_nights BETWEEN :nights_min AND :nights_max"

    if stops_bucket is not None:
        params["stops_bucket"] = stops_bucket
        sql += " AND stops_bucket = :stops_bucket"

    sql += " ORDER BY price"

    rows = conn.execute(sql, params).fetchall()
    return [row["price"] for row in rows]


def get_last_notification(
    conn: sqlite3.Connection,
    origin: str,
    destination: str,
    departure_date: str,
    return_date: Optional[str],
) -> Optional[sqlite3.Row]:
    """La derniere notification envoyee pour cette cle logique (origin, destination,
    departure_date, return_date), ou None si jamais notifiee. Pilote dedup.should_notify
    (jalon M6)."""
    return conn.execute(
        """
        SELECT * FROM notified_deals
        WHERE origin = :origin AND destination = :destination
          AND departure_date = :departure_date
          AND (return_date = :return_date OR (return_date IS NULL AND :return_date IS NULL))
        ORDER BY notified_at DESC
        LIMIT 1
        """,
        {
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "return_date": return_date,
        },
    ).fetchone()


def insert_notification(conn: sqlite3.Connection, deal: NotifiedDeal) -> int:
    cursor = conn.execute(
        """
        INSERT INTO notified_deals (
            notified_at, origin, destination, departure_date, return_date,
            price, currency, score, discount, observation_id
        ) VALUES (
            :notified_at, :origin, :destination, :departure_date, :return_date,
            :price, :currency, :score, :discount, :observation_id
        )
        """,
        asdict(deal),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def get_requests_used(conn: sqlite3.Connection, month: str) -> int:
    """month au format 'YYYY-MM'."""
    row = conn.execute("SELECT requests_used FROM api_usage WHERE month = ?", (month,)).fetchone()
    return row["requests_used"] if row else 0


def increment_requests_used(conn: sqlite3.Connection, month: str, count: int = 1) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO api_usage (month, requests_used, last_updated_at)
        VALUES (:month, :count, :now)
        ON CONFLICT(month) DO UPDATE SET
            requests_used = requests_used + :count,
            last_updated_at = :now
        """,
        {"month": month, "count": count, "now": now},
    )
