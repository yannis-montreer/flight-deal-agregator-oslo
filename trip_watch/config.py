"""Configuration minimaliste et dediee pour trip_watch — pas le meme niveau de generalite
que flightdeals.config, volontairement : ceci suit UN trajet precis, temporairement, pas un
produit multi-parametres destine a durer."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class TripWatchConfig:
    origin: str
    destination: str
    destination_name: str
    currency: str
    schedule: str
    departure_center: date
    departure_tolerance_days: int
    duration_center_days: int
    duration_tolerance_days: int
    api_key: str
    telegram_bot_token: str
    telegram_chat_id: str


def _default_config_path() -> Path:
    return Path(os.environ.get("TRIP_WATCH_CONFIG_PATH", "trip_watch/config.yaml"))


def load_config(path: "Path | str | None" = None) -> TripWatchConfig:
    config_path = Path(path) if path else _default_config_path()
    if not config_path.exists():
        raise ConfigError(f"Fichier de config introuvable: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # SEARCH_SERPAPI_KEY (cle dediee, recommandee) prioritaire sur SERPAPI_KEY (cle du
    # produit principal, en secours uniquement — consomme SON quota, voir README).
    api_key = (os.environ.get("SEARCH_SERPAPI_KEY") or os.environ.get("SERPAPI_KEY", "")).strip()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not api_key:
        raise ConfigError("SEARCH_SERPAPI_KEY (ou SERPAPI_KEY a defaut) manquant dans l'environnement")
    if not bot_token or not chat_id:
        raise ConfigError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquant dans l'environnement")

    try:
        return TripWatchConfig(
            origin=raw["origin"],
            destination=raw["destination"],
            destination_name=raw["destination_name"],
            currency=raw["currency"],
            schedule=raw["schedule"],
            departure_center=date.fromisoformat(raw["departure"]["center"]),
            departure_tolerance_days=raw["departure"]["tolerance_days"],
            duration_center_days=raw["duration"]["center_days"],
            duration_tolerance_days=raw["duration"]["tolerance_days"],
            api_key=api_key,
            telegram_bot_token=bot_token,
            telegram_chat_id=chat_id,
        )
    except KeyError as exc:
        raise ConfigError(f"Cle de config manquante dans {config_path}: {exc}") from exc
