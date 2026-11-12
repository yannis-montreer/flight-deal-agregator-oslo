"""Plusieurs veilles "bons plans" dans un seul service.

- La veille historique : config/config.yaml (identifiant "main"), base FLIGHTDEALS_DB_PATH
  (data/flightdeals.db) — strictement inchangee.
- Les autres : un fichier `deals/<nom>.yaml` chacune (meme format que config.yaml, plus
  `name`, `api_key_env`, `db_file`), creees depuis l'interface. Chacune a sa base, sa cle,
  son budget interne et son heure ; ses messages Telegram portent son nom.

Memes garanties de planification que les alertes trajet (flightdeals.scheduler.WatchScheduler) :
prochaine heure a venir au demarrage (jamais de double run apres un redemarrage), relecture
du dossier toutes les 5 minutes, jamais deux runs le meme jour, une veille en erreur ne
bloque pas les autres. `collection.enabled: false` = veille en pause.
"""
from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from flightdeals.config import Config, ConfigError, load_config
from flightdeals.scheduler import WatchScheduler

logger = logging.getLogger(__name__)

MAIN_ID = "main"
DEAL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


@dataclass(frozen=True)
class Job:
    """Adaptateur pour WatchScheduler (qui attend .schedule/.timezone/.enabled)."""
    config: Config

    @property
    def schedule(self) -> str:
        return self.config.collection.schedule

    @property
    def timezone(self) -> str:
        return self.config.collection.timezone

    @property
    def enabled(self) -> bool:
        return self.config.collection.enabled


def deal_files(deals_dir: Optional[Path]) -> list[Path]:
    if deals_dir is None or not deals_dir.is_dir():
        return []
    return [p for p in sorted(deals_dir.glob("*.yaml")) if DEAL_ID_RE.match(p.stem) and p.stem != MAIN_ID]


def load_veilles(main_path: Path, deals_dir: Optional[Path], *, require_secrets: bool = True) -> tuple[dict[str, Config], dict[str, str]]:
    configs: dict[str, Config] = {}
    errors: dict[str, str] = {}
    for veille_id, path in [(MAIN_ID, main_path)] + [(p.stem, p) for p in deal_files(deals_dir)]:
        try:
            config = load_config(path, require_secrets=require_secrets)
            configs[veille_id] = dataclasses.replace(config, veille_id=veille_id)
        except (ConfigError, OSError) as exc:
            errors[veille_id] = str(exc)
    return configs, errors


def db_path_for(config: Config, data_dir: Path, main_db_path: Path) -> Path:
    if config.db_file:
        return data_dir / config.db_file
    if config.veille_id == MAIN_ID:
        return main_db_path
    return data_dir / f"flightdeals_{config.veille_id}.db"


def run_cycle(
    scheduler: WatchScheduler,
    main_path: Path,
    deals_dir: Optional[Path],
    data_dir: Path,
    main_db_path: Path,
    run_once: Callable[[Config, Path], object],
    now: datetime,
    clock: Callable[[], datetime],
    previous_errors: Optional[dict[str, str]] = None,
) -> tuple[list[str], dict[str, str]]:
    configs, errors = load_veilles(main_path, deals_dir)
    for vid, message in errors.items():
        if (previous_errors or {}).get(vid) != message:
            logger.error("[%s] veille ignoree, fichier invalide : %s", vid, message)

    jobs = {vid: Job(c) for vid, c in configs.items()}
    for vid in scheduler.refresh(jobs, now):
        plan = scheduler.plans.get(vid)
        if plan:
            logger.info("[%s] prochain run le %s (%s heure %s)", vid,
                        plan.due_at.astimezone(ZoneInfo(jobs[vid].timezone)).strftime("%d/%m %H:%M"),
                        jobs[vid].schedule, jobs[vid].timezone)
        else:
            logger.info("[%s] veille retiree du planning (supprimee, en pause ou invalide)", vid)

    executed = []
    for vid in scheduler.due(now):
        job = jobs[vid]
        logger.info("[%s] run quotidien%s", vid, f" ({job.config.name})" if job.config.name else "")
        try:
            run_once(job.config, db_path_for(job.config, data_dir, main_db_path))
        except Exception:  # noqa: BLE001 - une veille en erreur ne bloque jamais les autres
            logger.exception("[%s] le run quotidien a echoue de maniere inattendue", vid)
        scheduler.mark_done(vid, job, clock())
        executed.append(vid)
        logger.info("[%s] prochain run le %s", vid,
                    scheduler.plans[vid].due_at.astimezone(ZoneInfo(job.timezone)).strftime("%d/%m %H:%M"))
    return executed, errors
