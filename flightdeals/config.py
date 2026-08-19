"""Chargement de la configuration : config fonctionnelle depuis config.yaml, secrets depuis
les variables d'environnement.

load_config() est appelee a chaque debut de run (voir scheduler.run_forever), jamais mise en
cache au demarrage du process : les seuils de config.yaml peuvent donc etre ajustes puis pris
en compte au prochain cycle sans rebuild de l'image Docker.

Validation fail-fast : une config invalide (poids qui ne somment pas a 1.0, format d'heure
invalide, variable d'environnement manquante, ...) leve ConfigError immediatement plutot que
de se rabattre silencieusement sur une valeur par defaut devinee. Sur un systeme qui tourne
sans supervision quotidienne, un crash visible dans `docker logs` vaut mieux qu'un comportement
silencieusement faux pendant des semaines.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

# Charge .env s'il existe (dev local hors Docker) ; no-op silencieux sinon. En conteneur, les
# secrets arrivent directement via `env_file` dans docker-compose — pas de fichier .env monte,
# donc cet appel ne fait rien la-bas, ce qui est le comportement voulu (voir README).
load_dotenv()

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(Exception):
    """config.yaml ou variables d'environnement manquantes/invalides."""


@dataclass(frozen=True)
class CollectionConfig:
    enabled: bool
    schedule: str  # "HH:MM", toujours UTC


@dataclass(frozen=True)
class SerpApiConfig:
    monthly_budget: int
    min_budget_reserve: int
    travel_durations: tuple
    arrival_area_id: Optional[str]
    month: Optional[int]  # 1-12 = Jan-Dec (enum SerpApi), None/absent = 6 prochains mois


@dataclass(frozen=True)
class DealConfig:
    minimum_discount: float
    minimum_observations: int
    percentile_threshold: float
    history_window_days: int
    duration_tolerance_nights: int
    max_duration_deviation_ratio: float
    min_trip_length_nights: int
    max_trip_length_nights: int


@dataclass(frozen=True)
class ScoringConfig:
    weights: dict
    discount_cap: float
    confidence_saturation_count: int
    directness_bonus: dict


@dataclass(frozen=True)
class DedupConfig:
    further_drop_threshold: float
    reappear_gap_days: int


@dataclass(frozen=True)
class NotificationConfig:
    telegram: bool
    send_delay_seconds: float
    daily_summary_enabled: bool


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    max_bytes: int
    backup_count: int


@dataclass(frozen=True)
class Secrets:
    serpapi_key: str
    telegram_bot_token: str
    telegram_chat_id: str


@dataclass(frozen=True)
class Config:
    origin: str
    currency: str
    collection: CollectionConfig
    serpapi: SerpApiConfig
    deal: DealConfig
    scoring: ScoringConfig
    dedup: DedupConfig
    notification: NotificationConfig
    logging: LoggingConfig
    secrets: Secrets


def _default_config_path() -> Path:
    return Path(os.environ.get("FLIGHTDEALS_CONFIG_PATH", "config/config.yaml"))


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Variable d'environnement requise manquante: {name}. "
            f"Voir .env.example pour la liste complete."
        )
    return value


def _load_secrets() -> Secrets:
    return Secrets(
        serpapi_key=_require_env("SERPAPI_KEY"),
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_require_env("TELEGRAM_CHAT_ID"),
    )


def load_config(path: "Path | str | None" = None) -> Config:
    config_path = Path(path) if path else _default_config_path()
    if not config_path.exists():
        raise ConfigError(f"Fichier de config introuvable: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    try:
        collection = CollectionConfig(**raw["collection"])
        serpapi = SerpApiConfig(
            monthly_budget=raw["serpapi"]["monthly_budget"],
            min_budget_reserve=raw["serpapi"]["min_budget_reserve"],
            travel_durations=tuple(raw["serpapi"]["travel_durations"]),
            arrival_area_id=raw["serpapi"].get("arrival_area_id"),
            month=raw["serpapi"].get("month"),
        )
        deal = DealConfig(**raw["deal"])
        scoring = ScoringConfig(
            weights=dict(raw["scoring"]["weights"]),
            discount_cap=raw["scoring"]["discount_cap"],
            confidence_saturation_count=raw["scoring"]["confidence_saturation_count"],
            directness_bonus=dict(raw["scoring"]["directness_bonus"]),
        )
        dedup = DedupConfig(**raw["dedup"])
        notification = NotificationConfig(**raw["notification"])
        logging_cfg = LoggingConfig(**raw["logging"])
        origin = raw["origin"]
        currency = raw["currency"]
    except KeyError as exc:
        raise ConfigError(f"Cle de configuration manquante dans {config_path}: {exc}") from exc
    except TypeError as exc:
        raise ConfigError(f"Configuration mal formee dans {config_path}: {exc}") from exc

    if not _HHMM_RE.match(collection.schedule):
        raise ConfigError(
            f"collection.schedule doit etre au format 'HH:MM' (UTC, 24h), recu: {collection.schedule!r}"
        )

    weights_sum = sum(scoring.weights.values())
    if abs(weights_sum - 1.0) > 1e-6:
        raise ConfigError(f"scoring.weights doit sommer a 1.0, somme actuelle: {weights_sum}")

    if not (0.0 < deal.minimum_discount < 1.0):
        raise ConfigError(f"deal.minimum_discount doit etre entre 0 et 1, recu: {deal.minimum_discount}")

    if not (0.0 < deal.percentile_threshold < 1.0):
        raise ConfigError(
            f"deal.percentile_threshold doit etre entre 0 et 1, recu: {deal.percentile_threshold}"
        )

    if deal.minimum_observations < 1:
        raise ConfigError("deal.minimum_observations doit etre >= 1")

    if deal.max_duration_deviation_ratio < 0:
        raise ConfigError(
            f"deal.max_duration_deviation_ratio doit etre >= 0, recu: {deal.max_duration_deviation_ratio}"
        )

    if serpapi.month is not None and not (1 <= serpapi.month <= 12):
        raise ConfigError(f"serpapi.month doit etre entre 1 et 12 (ou absent), recu: {serpapi.month}")

    if deal.min_trip_length_nights < 0 or deal.max_trip_length_nights < deal.min_trip_length_nights:
        raise ConfigError(
            f"deal.min_trip_length_nights ({deal.min_trip_length_nights}) doit etre >= 0 et "
            f"<= deal.max_trip_length_nights ({deal.max_trip_length_nights})"
        )

    if scoring.confidence_saturation_count <= deal.minimum_observations:
        raise ConfigError(
            "scoring.confidence_saturation_count doit etre strictement superieur a "
            "deal.minimum_observations (sinon la rampe de confiance est degeneree)"
        )

    secrets = _load_secrets()

    return Config(
        origin=origin,
        currency=currency,
        collection=collection,
        serpapi=serpapi,
        deal=deal,
        scoring=scoring,
        dedup=dedup,
        notification=notification,
        logging=logging_cfg,
        secrets=secrets,
    )
