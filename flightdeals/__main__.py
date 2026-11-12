"""Entrypoint du conteneur (ENTRYPOINT ["python", "-m", "flightdeals"] dans le Dockerfile).
Fait tourner la veille historique (config/config.yaml) et les veilles du dossier
FLIGHTDEALS_DEALS_DIR (deals/), voir multi.py. Auto-suffisant, aucun cron externe requis.

Variables d'environnement :
- FLIGHTDEALS_CONFIG_PATH : veille historique (defaut: config/config.yaml)
- FLIGHTDEALS_DB_PATH : base de la veille historique (defaut: data/flightdeals.db)
- FLIGHTDEALS_DEALS_DIR : dossier des autres veilles (absent = veille historique seule)
- FLIGHTDEALS_DATA_DIR : dossier des bases des autres veilles (defaut: dossier de FLIGHTDEALS_DB_PATH)
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from flightdeals import multi
from flightdeals.config import load_config
from flightdeals.logging_setup import setup_logging
from flightdeals.pipeline import run_once
from flightdeals.scheduler import WatchScheduler

logger = logging.getLogger(__name__)


def main() -> None:
    # Un premier chargement, juste pour configurer le logging le plus tot possible. Si CE
    # chargement echoue (cle API manquante, config.yaml invalide...), le crash doit etre
    # immediat et visible dans `docker logs` — pas de degradation silencieuse au demarrage.
    initial_config = load_config()
    setup_logging(
        level=initial_config.logging.level,
        max_bytes=initial_config.logging.max_bytes,
        backup_count=initial_config.logging.backup_count,
    )

    main_path = Path(os.environ.get("FLIGHTDEALS_CONFIG_PATH", "config/config.yaml"))
    main_db_path = Path(os.environ.get("FLIGHTDEALS_DB_PATH", "data/flightdeals.db"))
    deals_env = os.environ.get("FLIGHTDEALS_DEALS_DIR")
    deals_dir = Path(deals_env) if deals_env else None
    data_dir = Path(os.environ.get("FLIGHTDEALS_DATA_DIR", str(main_db_path.parent)))

    configs, errors = multi.load_veilles(main_path, deals_dir)
    logger.info("flightdeals demarre : %d veille(s) (%d invalide(s))", len(configs) + len(errors), len(errors))
    for vid, config in sorted(configs.items()):
        logger.info(
            "[%s] %s : zone %s, mois %s, cle %s, base %s%s", vid, config.name or "veille historique",
            config.serpapi.arrival_area_id or "partout", config.serpapi.month or "6 prochains",
            config.api_key_env, multi.db_path_for(config, data_dir, main_db_path).name,
            "" if config.collection.enabled else " (EN PAUSE)",
        )

    scheduler = WatchScheduler()
    previous_errors: dict[str, str] = {}
    while True:
        now = datetime.now(timezone.utc)
        _, previous_errors = multi.run_cycle(
            scheduler, main_path, deals_dir, data_dir, main_db_path, run_once, now,
            lambda: datetime.now(timezone.utc), previous_errors,
        )
        time.sleep(scheduler.seconds_to_sleep(datetime.now(timezone.utc)))


if __name__ == "__main__":
    main()
