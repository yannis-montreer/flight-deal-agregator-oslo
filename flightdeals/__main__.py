"""Entrypoint du conteneur (ENTRYPOINT ["python", "-m", "flightdeals"] dans le Dockerfile,
jalon M10). Charge la config, initialise le logging, puis lance la boucle de scheduling
interne — auto-suffisant, aucun cron externe requis (voir scheduler.py).
"""
from __future__ import annotations

import os
from pathlib import Path

from flightdeals.config import load_config
from flightdeals.logging_setup import setup_logging
from flightdeals.pipeline import run_once
from flightdeals.scheduler import run_forever


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

    db_path = Path(os.environ.get("FLIGHTDEALS_DB_PATH", "data/flightdeals.db"))

    run_forever(
        run_once_fn=lambda config: run_once(config, db_path),
        load_config_fn=load_config,
    )


if __name__ == "__main__":
    main()
