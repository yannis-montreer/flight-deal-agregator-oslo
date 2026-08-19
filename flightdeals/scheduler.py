"""Boucle de scheduling interne, auto-suffisante : le conteneur n'a besoin d'aucun cron
externe (decision de deploiement Docker, voir plan). Implementation stdlib pure — pas de lib
de scheduler (APScheduler, `schedule`) : un seul job quotidien ne le justifie pas.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def seconds_until_next_run(now: datetime, target_hhmm: str, timezone_name: str = "UTC") -> float:
    """target_hhmm au format 'HH:MM', interprete dans le fuseau `timezone_name` (nom IANA,
    ex: 'Europe/Oslo'). Par defaut UTC (comportement historique, toujours utilise par
    trip_watch qui n'a pas de notion de fuseau local — voir plan : "jamais d'heure locale
    naive"). `now` DOIT etre timezone-aware (n'importe quel fuseau, generalement UTC).

    zoneinfo (stdlib, pas de dependance externe) gere automatiquement le changement heure
    ete/hiver : "10:30" avec timezone_name="Europe/Oslo" correspond a 08:30 UTC en ete (CEST,
    UTC+2) et 09:30 UTC en hiver (CET, UTC+1), sans ajustement manuel ni derive au fil des
    changements d'heure. La soustraction de deux datetimes aware (meme fuseau) reste exacte
    en heures reelles ecoulees meme si un changement d'heure tombe entre les deux (Python
    calcule via l'instant UTC absolu, pas une difference d'horloge murale naive).

    Hypothese implicite : target_hhmm ne tombe jamais dans l'heure ambigue/inexistante d'une
    transition DST (en Europe, les transitions ont lieu vers 1h-3h du matin local — sans
    consequence pour un horaire de milieu de matinee comme 10:30)."""
    hour, minute = map(int, target_hhmm.split(":"))
    tz = ZoneInfo(timezone_name)
    now_in_tz = now.astimezone(tz)
    candidate_in_tz = now_in_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate_in_tz <= now_in_tz:
        candidate_in_tz += timedelta(days=1)
    return (candidate_in_tz - now_in_tz).total_seconds()


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
        wait_seconds = seconds_until_next_run(now, config.collection.schedule, config.collection.timezone)
        logger.info(
            "Prochain run dans %.0fs (a %s heure %s)",
            wait_seconds, config.collection.schedule, config.collection.timezone,
        )
        time.sleep(wait_seconds)

        config = load_config_fn()  # relu : peut avoir change pendant le sleep (potentiellement long)
        if not config.collection.enabled:
            logger.info("Collecte desactivee dans la config (collection.enabled=false) — run saute")
            continue

        try:
            run_once_fn(config)
        except Exception:
            logger.exception("Le run quotidien a echoue de maniere inattendue")
