"""Entrypoint trip_watch : `python -m trip_watch`. Reutilise seconds_until_next_run du
produit principal (fonction pure, deja testee) plutot que de reimplementer une boucle de
scheduling — mais reste une boucle dediee, plus simple que flightdeals.scheduler.run_forever
(qui attend une forme de config specifique non partagee ici)."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from flightdeals.logging_setup import setup_logging
from flightdeals.scheduler import seconds_until_next_run

from trip_watch.config import load_config
from trip_watch.tracker import run_daily_check

logger = logging.getLogger(__name__)


def main() -> None:
    # Premier chargement pour configurer le logging au plus tot ; un echec ici (cle/token
    # manquant, config.yaml invalide) doit crasher immediatement et lisiblement.
    initial_config = load_config()
    setup_logging(level="INFO")
    logger.info(
        "trip_watch demarre: %s -> %s, depart %s +/-%dj, sejour %dj +/-%dj",
        initial_config.origin, initial_config.destination, initial_config.departure_center,
        initial_config.departure_tolerance_days, initial_config.duration_center_days,
        initial_config.duration_tolerance_days,
    )

    db_path = Path(os.environ.get("TRIP_WATCH_DB_PATH", "data/trip_watch.db"))

    while True:
        config = load_config()  # relu a chaque iteration, memes garanties que le produit principal
        now = datetime.now(timezone.utc)
        wait_seconds = seconds_until_next_run(now, config.schedule, config.timezone)
        logger.info("Prochaine verification dans %.0fs (a %s heure %s)", wait_seconds, config.schedule, config.timezone)
        time.sleep(wait_seconds)

        config = load_config()  # relu une 2e fois : peut avoir change pendant le sleep
        try:
            run_daily_check(config, db_path)
        except Exception:
            logger.exception("La verification quotidienne a echoue de maniere inattendue")


if __name__ == "__main__":
    main()
