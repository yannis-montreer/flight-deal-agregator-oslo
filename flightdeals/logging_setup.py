"""Configuration du logging : stdout (capture par `docker logs`) + fichier rotatif sur le
volume de donnees persistant (pour diagnostiquer une erreur meme apres que les logs stdout
du conteneur aient ete perdus/tournes par le runtime Docker).
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str = "INFO", max_bytes: int = 5_242_880, backup_count: int = 5) -> None:
    """A appeler une seule fois, au tout debut du process, avant tout autre log."""
    log_dir = Path(os.environ.get("FLIGHTDEALS_LOG_DIR", "data/logs"))
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    formatter = logging.Formatter(_FORMAT)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    file_handler = RotatingFileHandler(
        log_dir / "flightdeals.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # httpx logue une ligne par requete a INFO ; a un volume de ~3 req/jour ce n'est pas geant,
    # mais autant garder le signal-bruit propre des le depart.
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("httpcore").setLevel("WARNING")
