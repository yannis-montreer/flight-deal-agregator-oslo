"""Estimation du cout SerpApi mensuel de chaque veille, regroupe par cle.

Pire cas sur un mois de 31 jours : c'est le seul chiffre qui garantit de ne jamais vider
un quota avant la fin du mois. Les appels account.json (gratuits) ne comptent pas.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from flightdeals.config import Config as FlightDealsConfig
from trip_watch.config import TripWatchConfig
from webui.i18n import Msg

DAYS_PER_MONTH = 31
FREE_PLAN_QUOTA = 250  # forfait gratuit SerpApi (recherches/mois)
FLIGHTDEALS_KEY_ENV = "SERPAPI_KEY"


class BudgetError(Exception):
    """Le pire cas mensuel depasse le quota d'une cle : enregistrement refuse."""


@dataclass(frozen=True)
class WatchCost:
    watch_id: str
    label: str
    key_env: str
    per_day: int
    per_month: int
    detail: str


def deal_cost_id(veille_id: str) -> str:
    return "flightdeals" if veille_id == "main" else f"deal:{veille_id}"


def deal_label(config: FlightDealsConfig) -> str:
    return config.name or "Bons plans (flightdeals)"


def flightdeals_cost(config: FlightDealsConfig, veille_id: str = "main") -> WatchCost:
    key_env = getattr(config, "api_key_env", FLIGHTDEALS_KEY_ENV)
    cost_id, label = deal_cost_id(veille_id), deal_label(config)
    if not config.collection.enabled:
        return WatchCost(cost_id, label, key_env, 0, 0, Msg("en pause : aucune recherche"))
    explore = len(config.serpapi.travel_durations)
    cold = config.cold_start_check.max_candidates_per_run if config.cold_start_check.enabled else 0
    per_day = explore + cold
    raw_month = per_day * DAYS_PER_MONTH
    # Le service s'arrete de lui-meme a monthly_budget (runs sautes ensuite, voir pipeline).
    per_month = min(raw_month, config.serpapi.monthly_budget)
    template = "{explore} recherche(s) d'exploration/jour"
    if cold:
        template += " + jusqu'à {cold} vérification(s) Google (démarrage à froid)"
    if raw_month > config.serpapi.monthly_budget:
        template += " ; plafonné par le budget interne de {budget}/mois (des runs seront sautés en fin de mois)"
    detail = Msg(template, explore=explore, cold=cold, budget=config.serpapi.monthly_budget)
    return WatchCost(cost_id, label, key_env, per_day, per_month, detail)


def trip_watch_cost(watch_id: str, config: TripWatchConfig) -> WatchCost:
    if not getattr(config, "enabled", True):
        return WatchCost(watch_id, config.destination_name, config.api_key_env, 0, 0, Msg("en pause : aucune recherche"))
    per_day = 2 * config.departure_tolerance_days + 1
    detail = Msg("{n} date(s) de départ testée(s)/jour", n=per_day)
    return WatchCost(watch_id, config.destination_name, config.api_key_env, per_day, per_day * DAYS_PER_MONTH, detail)


def key_quotas() -> dict[str, int]:
    """Quota mensuel par NOM de cle. Par defaut le forfait gratuit ; surchargeable via
    WEBUI_KEY_QUOTAS="SERPAPI_KEY=250,SERPAPI_KEY_3=1000" (forfait payant, par ex.)."""
    quotas: dict[str, int] = {}
    for item in os.environ.get("WEBUI_KEY_QUOTAS", "").split(","):
        if "=" in item:
            name, value = item.split("=", 1)
            if value.strip().isdigit():
                quotas[name.strip()] = int(value.strip())
    return quotas


def quota_for(key_env: str, registry: Optional[dict] = None) -> int:
    """Quota mensuel d'une cle, par ordre de priorite : surcharge WEBUI_KEY_QUOTAS, puis le
    forfait reel lu par le service des alertes (registre, account.json), puis 250 (gratuit)."""
    override = key_quotas().get(key_env)
    if override is not None:
        return override
    for entry in (registry or {}).get("keys", []):
        if entry.get("name") == key_env and isinstance(entry.get("searches_per_month"), int):
            return entry["searches_per_month"]
    return FREE_PLAN_QUOTA


def totals_by_key(costs: list[WatchCost]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for cost in costs:
        totals[cost.key_env] = totals.get(cost.key_env, 0) + cost.per_month
    return totals


def check_budget(costs: list[WatchCost], registry: Optional[dict] = None) -> None:
    for key_env, total in totals_by_key(costs).items():
        quota = quota_for(key_env, registry)
        if total > quota:
            users = ", ".join(c.label for c in costs if c.key_env == key_env)
            raise BudgetError(Msg(
                "La clé {key} serait sollicitée jusqu'à {total} recherches/mois ({users}), "
                "au-delà de son quota de {quota}/mois. Réduis la plage de dates ou le nombre de recherches.",
                key=key_env, total=total, users=users, quota=quota,
            ))
