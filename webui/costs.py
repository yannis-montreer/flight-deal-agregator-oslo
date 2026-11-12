"""Estimation du cout SerpApi mensuel de chaque veille, regroupe par cle.

Pire cas sur un mois de 31 jours : c'est le seul chiffre qui garantit de ne jamais vider
un quota avant la fin du mois. Les appels account.json (gratuits) ne comptent pas.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from flightdeals.config import Config as FlightDealsConfig
from trip_watch.config import TripWatchConfig

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


def flightdeals_cost(config: FlightDealsConfig) -> WatchCost:
    explore = len(config.serpapi.travel_durations)
    cold = config.cold_start_check.max_candidates_per_run if config.cold_start_check.enabled else 0
    per_day = explore + cold
    raw_month = per_day * DAYS_PER_MONTH
    # Le service s'arrete de lui-meme a monthly_budget (runs sautes ensuite, voir pipeline).
    per_month = min(raw_month, config.serpapi.monthly_budget)
    detail = f"{explore} recherche(s) d'exploration/jour"
    if cold:
        detail += f" + jusqu'à {cold} vérification(s) Google (démarrage à froid)"
    if raw_month > config.serpapi.monthly_budget:
        detail += f" ; plafonné par le budget interne de {config.serpapi.monthly_budget}/mois (des runs seront sautés en fin de mois)"
    return WatchCost("flightdeals", "Bons plans Asie (flightdeals)", FLIGHTDEALS_KEY_ENV, per_day, per_month, detail)


def trip_watch_cost(watch_id: str, config: TripWatchConfig) -> WatchCost:
    per_day = 2 * config.departure_tolerance_days + 1
    detail = f"{per_day} date(s) de départ testée(s)/jour"
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


def quota_for(key_env: str) -> int:
    return key_quotas().get(key_env, FREE_PLAN_QUOTA)


def totals_by_key(costs: list[WatchCost]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for cost in costs:
        totals[cost.key_env] = totals.get(cost.key_env, 0) + cost.per_month
    return totals


def check_budget(costs: list[WatchCost]) -> None:
    for key_env, total in totals_by_key(costs).items():
        quota = quota_for(key_env)
        if total > quota:
            users = ", ".join(c.label for c in costs if c.key_env == key_env)
            raise BudgetError(
                f"La clé {key_env} serait sollicitée jusqu'à {total} recherches/mois ({users}), "
                f"au-delà de son quota de {quota}/mois. Réduis la plage de dates ou le nombre de recherches."
            )
