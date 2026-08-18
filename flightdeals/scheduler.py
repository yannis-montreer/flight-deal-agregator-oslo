"""Boucle de scheduling interne, auto-suffisante : le conteneur n'a besoin d'aucun cron
externe (decision de deploiement Docker, voir plan). Implementation stdlib pure — pas de lib
de scheduler (APScheduler, `schedule`) : un seul job quotidien ne le justifie pas.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)


def seconds_until_next_run(now: datetime, target_hhmm: str) -> float:
    """target_hhmm au format 'HH:MM', toujours interprete en UTC. `now` DOIT etre
    timezone-aware UTC (voir plan : jamais d'heure locale naive dans ce projet)."""
    hour, minute = map(int, target_hhmm.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return (candidate - now).total_seconds()


def run_forever(run_once_fn: Callable[[Any], None], load_config_fn: Callable[[], Any]) -> None:
    """Boucle infinie : dort jusqu'a l'heure configuree, execute run_once_fn, recommence.

    La config est rechargee a CHAQUE iteration (avant ET apres le sleep) pour permettre de
    modifier les seuils, l'heure planifiee, ou de desactiver la collecte (collection.enabled)
    en editant config.yaml, sans rebuild ni redemarrage du conteneur.

    Propriete importante (voir plan) : apres que run_once_fn ait retourne — potentiellement
    plusieurs minutes plus tard — seconds_until_next_run est recalcule depuis l'heure REELLE.
    `now` a alors deja depasse l'heure cible du jour, donc `candidate <= now` bascule
    automatiquement sur demain : aucun cas particulier a coder pour distinguer "on vient de
    finir un run" de "on n'a encore jamais tourne aujourd'hui".

    Une exception non geree dans run_once_fn est loguee mais ne fait JAMAIS crasher la
    boucle : le prochain cycle est tente normalement le lendemain (spec section 14 :
    fiabilite, une panne ponctuelle ne doit pas empecher les jours suivants)."""
    while True:
        config = load_config_fn()
        now = datetime.now(timezone.utc)
        wait_seconds = seconds_until_next_run(now, config.collection.schedule)
        logger.info("Prochain run dans %.0fs (a %s UTC)", wait_seconds, config.collection.schedule)
        time.sleep(wait_seconds)

        config = load_config_fn()  # relu : peut avoir change pendant le sleep (potentiellement long)
        if not config.collection.enabled:
            logger.info("Collecte desactivee dans la config (collection.enabled=false) — run saute")
            continue

        try:
            run_once_fn(config)
        except Exception:
            logger.exception("Le run quotidien a echoue de maniere inattendue")
