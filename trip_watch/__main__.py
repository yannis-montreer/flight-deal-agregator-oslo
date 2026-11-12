"""Entrypoint trip_watch : `python -m trip_watch`. Fait tourner TOUTES les alertes du dossier
TRIP_WATCH_DIR (une par fichier .yaml), voir runner.py pour les garanties de planification.

Variables d'environnement :
- TRIP_WATCH_DIR : dossier des alertes (defaut: watches)
- TRIP_WATCH_DATA_DIR : dossier des bases SQLite (defaut: data)
- TRIP_WATCH_STATUS_DIR : dossier ou publier le registre des cles pour l'interface
  (noms + compteurs, jamais de secret) ; absent = pas de publication
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from flightdeals.collectors.serpapi_client import SerpApiClient
from flightdeals.logging_setup import setup_logging

from trip_watch import keys
from trip_watch.runner import WatchScheduler, load_watches, run_cycle, utc_now
from trip_watch.tracker import run_daily_check

logger = logging.getLogger(__name__)


def _fetch_account(api_key: str) -> dict:
    with SerpApiClient(api_key=api_key) as client:
        return client.get_account_info()  # gratuit : ne consomme aucune recherche


def main() -> None:
    setup_logging(level="INFO")
    watch_dir = Path(os.environ.get("TRIP_WATCH_DIR", "watches"))
    data_dir = Path(os.environ.get("TRIP_WATCH_DATA_DIR", "data"))
    status_env = os.environ.get("TRIP_WATCH_STATUS_DIR")
    status_dir = Path(status_env) if status_env else None

    configs, errors = load_watches(watch_dir)
    logger.info(
        "trip_watch demarre : %d alerte(s) dans %s (%d active(s), %d invalide(s))",
        len(configs) + len(errors), watch_dir, sum(c.enabled for c in configs.values()), len(errors),
    )
    for wid, config in sorted(configs.items()):
        logger.info(
            "[%s] %s -> %s, depart %s +/-%dj, sejour %dj +/-%dj, cle %s%s", wid,
            config.origin, config.destination, config.departure_center, config.departure_tolerance_days,
            config.duration_center_days, config.duration_tolerance_days, config.api_key_env,
            "" if config.enabled else " (EN PAUSE)",
        )
    keys.publish(status_dir, _fetch_account)
    last_publish = time.monotonic()

    scheduler = WatchScheduler()
    previous_errors: dict[str, str] = {}
    while True:
        now = utc_now()
        executed, previous_errors = run_cycle(
            scheduler, watch_dir, data_dir, run_daily_check, now, utc_now, previous_errors
        )
        if executed or time.monotonic() - last_publish >= keys.REFRESH_SECONDS:
            keys.publish(status_dir, _fetch_account, quiet=not executed)
            last_publish = time.monotonic()
        time.sleep(scheduler.seconds_to_sleep(utc_now()))


if __name__ == "__main__":
    main()
