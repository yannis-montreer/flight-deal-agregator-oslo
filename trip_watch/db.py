"""Base SQLite legere et dediee a trip_watch — totalement independante de flightdeals.db
(pas de partage d'etat avec le produit principal). Une seule table : une ligne par
observation quotidienne."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at    TEXT    NOT NULL,
    destination    TEXT    NOT NULL,
    departure_date TEXT    NOT NULL,
    return_date    TEXT    NOT NULL,
    price          REAL,
    currency       TEXT    NOT NULL,
    airline        TEXT,
    stops          INTEGER,
    price_level    TEXT,   -- 'low' | 'typical' | 'high' (evaluation de Google, si fournie)
    error          TEXT    -- non-NULL si la requete a echoue / aucun vol trouve
);

CREATE INDEX IF NOT EXISTS idx_obs_key ON observations (destination, departure_date, return_date);
"""


def get_connection(db_path: "Path | str") -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    return conn


def insert_observation(
    conn: sqlite3.Connection,
    *,
    observed_at: str,
    destination: str,
    departure_date: str,
    return_date: str,
    price: Optional[float],
    currency: str,
    airline: Optional[str],
    stops: Optional[int],
    price_level: Optional[str],
    error: Optional[str],
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO observations (
            observed_at, destination, departure_date, return_date,
            price, currency, airline, stops, price_level, error
        ) VALUES (
            :observed_at, :destination, :departure_date, :return_date,
            :price, :currency, :airline, :stops, :price_level, :error
        )
        """,
        dict(
            observed_at=observed_at, destination=destination, departure_date=departure_date,
            return_date=return_date, price=price, currency=currency, airline=airline,
            stops=stops, price_level=price_level, error=error,
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def get_historical_min(
    conn: sqlite3.Connection,
    destination: str,
    departure_date: str,
    return_date: str,
    *,
    exclude_id: Optional[int] = None,
) -> Optional[float]:
    """Prix minimum deja observe pour cette EXACTE combinaison (destination + dates),
    hors l'observation exclude_id elle-meme (typiquement celle qu'on vient d'inserer).
    None si aucune observation anterieure avec prix pour cette combinaison."""
    sql = (
        "SELECT MIN(price) AS min_price FROM observations "
        "WHERE destination = :destination AND departure_date = :departure_date "
        "AND return_date = :return_date AND price IS NOT NULL"
    )
    params = {"destination": destination, "departure_date": departure_date, "return_date": return_date}
    if exclude_id is not None:
        sql += " AND id != :exclude_id"
        params["exclude_id"] = exclude_id

    row = conn.execute(sql, params).fetchone()
    return row["min_price"] if row and row["min_price"] is not None else None
