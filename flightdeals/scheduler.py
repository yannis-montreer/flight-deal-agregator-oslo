"""Boucle de scheduling interne, auto-suffisante : le conteneur n'a besoin d'aucun cron
externe (decision de deploiement Docker, voir plan). Implementation stdlib pure — pas de lib
de scheduler (APScheduler, `schedule`) : un seul job quotidien ne le justifie pas.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from datetime import date
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


# ---------- Planification de plusieurs jobs (veilles flightdeals, alertes trip_watch) ----------
# Chaque job est un objet ayant .schedule ("HH:MM"), .timezone (IANA) et .enabled.

RESCAN_SECONDS = 300


def _local_date(moment: datetime, tz_name: str) -> date:
    return moment.astimezone(ZoneInfo(tz_name)).date()


@dataclass
class _Plan:
    schedule_key: tuple
    due_at: datetime


class WatchScheduler:
    def __init__(self) -> None:
        self.plans: dict[str, _Plan] = {}
        self.last_run_date: dict[str, date] = {}

    def _next_due(self, watch_id: str, config: Any, now: datetime) -> datetime:
        due = now + timedelta(seconds=seconds_until_next_run(now, config.schedule, config.timezone))
        if self.last_run_date.get(watch_id) == _local_date(due, config.timezone):
            due += timedelta(days=1)  # deja tourne aujourd'hui : jamais deux fois le meme jour
        return due

    def refresh(self, configs: dict[str, Any], now: datetime) -> list[str]:
        """Met a jour le plan ; renvoie les alertes dont la planification a change."""
        changed = []
        active = {wid: c for wid, c in configs.items() if c.enabled}
        for wid in list(self.plans):
            if wid not in active:
                del self.plans[wid]
                changed.append(wid)
        for wid, config in active.items():
            key = (config.schedule, config.timezone)
            plan = self.plans.get(wid)
            if plan is None or plan.schedule_key != key:
                self.plans[wid] = _Plan(key, self._next_due(wid, config, now))
                changed.append(wid)
        return changed

    def due(self, now: datetime) -> list[str]:
        return sorted(wid for wid, plan in self.plans.items() if plan.due_at <= now)

    def mark_done(self, watch_id: str, config: Any, finished_at: datetime) -> None:
        self.last_run_date[watch_id] = _local_date(self.plans[watch_id].due_at, config.timezone)
        self.plans[watch_id] = _Plan((config.schedule, config.timezone), self._next_due(watch_id, config, finished_at))

    def seconds_to_sleep(self, now: datetime, cap: float = RESCAN_SECONDS) -> float:
        if not self.plans:
            return cap
        soonest = min(plan.due_at for plan in self.plans.values())
        return max(1.0, min(cap, (soonest - now).total_seconds()))
