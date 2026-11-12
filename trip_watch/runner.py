"""Execution de TOUTES les alertes du dossier watches/ par un seul service.

Ajouter une alerte = deposer un fichier `watches/<nom>.yaml` (l'interface le fait) : aucun
changement Docker. Chaque alerte a sa propre heure, sa propre cle SerpApi et sa propre base.

Garanties de planification (memes que l'ancien service mono-alerte, voir __main__) :
- au demarrage, chaque alerte est planifiee a sa PROCHAINE heure a venir : un redemarrage a
  10:31 ne relance pas le run de 10:30 deja fait (jamais de double run, donc jamais de
  message Telegram ni de quota consommes deux fois) ;
- le dossier est relu toutes les quelques minutes : une alerte ajoutee, mise en pause,
  supprimee ou dont l'heure change est prise en compte sans redemarrage ;
- une alerte ne tourne jamais deux fois le meme jour (heure locale de l'alerte), meme si son
  heure est decalee plus tard apres son run du jour ;
- une alerte invalide ou en erreur n'empeche jamais les autres de tourner.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from flightdeals.scheduler import seconds_until_next_run

from trip_watch.config import ConfigError, TripWatchConfig, load_config

logger = logging.getLogger(__name__)

WATCH_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
RESCAN_SECONDS = 300


def watch_files(watch_dir: Path) -> list[Path]:
    """Fichiers d'alerte actifs : `<nom>.yaml` directement dans le dossier (les sous-dossiers
    .history/ et .archive/ de l'interface, et les fichiers temporaires, sont ignores)."""
    return [p for p in sorted(watch_dir.glob("*.yaml")) if WATCH_ID_RE.match(p.stem)]


def load_watches(watch_dir: Path, *, require_secrets: bool = True) -> tuple[dict[str, TripWatchConfig], dict[str, str]]:
    configs, errors = {}, {}
    for path in watch_files(watch_dir):
        try:
            configs[path.stem] = load_config(path, require_secrets=require_secrets)
        except (ConfigError, OSError) as exc:
            errors[path.stem] = str(exc)
    return configs, errors


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

    def _next_due(self, watch_id: str, config: TripWatchConfig, now: datetime) -> datetime:
        due = now + timedelta(seconds=seconds_until_next_run(now, config.schedule, config.timezone))
        if self.last_run_date.get(watch_id) == _local_date(due, config.timezone):
            due += timedelta(days=1)  # deja tourne aujourd'hui : jamais deux fois le meme jour
        return due

    def refresh(self, configs: dict[str, TripWatchConfig], now: datetime) -> list[str]:
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

    def mark_done(self, watch_id: str, config: TripWatchConfig, finished_at: datetime) -> None:
        self.last_run_date[watch_id] = _local_date(self.plans[watch_id].due_at, config.timezone)
        self.plans[watch_id] = _Plan((config.schedule, config.timezone), self._next_due(watch_id, config, finished_at))

    def seconds_to_sleep(self, now: datetime, cap: float = RESCAN_SECONDS) -> float:
        if not self.plans:
            return cap
        soonest = min(plan.due_at for plan in self.plans.values())
        return max(1.0, min(cap, (soonest - now).total_seconds()))


def run_cycle(
    scheduler: WatchScheduler,
    watch_dir: Path,
    data_dir: Path,
    run_check: Callable[[TripWatchConfig, Path], None],
    now: datetime,
    clock: Callable[[], datetime],
    previous_errors: Optional[dict[str, str]] = None,
) -> tuple[list[str], dict[str, str]]:
    """Un tour de boucle : relit le dossier, replanifie, execute les alertes echues.
    Renvoie (alertes executees, erreurs de chargement)."""
    configs, errors = load_watches(watch_dir)
    for wid, message in errors.items():
        if (previous_errors or {}).get(wid) != message:
            logger.error("[%s] alerte ignoree, fichier invalide : %s", wid, message)

    for wid in scheduler.refresh(configs, now):
        plan = scheduler.plans.get(wid)
        if plan:
            logger.info("[%s] prochaine verification le %s (%s heure %s)", wid,
                        plan.due_at.astimezone(ZoneInfo(configs[wid].timezone)).strftime("%d/%m %H:%M"),
                        configs[wid].schedule, configs[wid].timezone)
        else:
            logger.info("[%s] alerte retiree du planning (supprimee, en pause ou invalide)", wid)

    executed = []
    for wid in scheduler.due(now):
        config = configs[wid]
        logger.info("[%s] verification quotidienne (%s -> %s)", wid, config.origin, config.destination)
        try:
            run_check(config, data_dir / config.db_file)
        except Exception:  # noqa: BLE001 - une alerte en erreur ne bloque jamais les autres
            logger.exception("[%s] la verification a echoue de maniere inattendue", wid)
        scheduler.mark_done(wid, config, clock())
        executed.append(wid)
        plan = scheduler.plans[wid]
        logger.info("[%s] prochaine verification le %s", wid,
                    plan.due_at.astimezone(ZoneInfo(config.timezone)).strftime("%d/%m %H:%M"))
    return executed, errors


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
