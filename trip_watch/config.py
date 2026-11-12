"""Configuration minimaliste et dediee pour trip_watch — pas le meme niveau de generalite
que flightdeals.config, volontairement : ceci suit UN trajet precis, temporairement, pas un
produit multi-parametres destine a durer."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    pass


_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_IATA_RE = re.compile(r"^[A-Z]{3}$")
# Nom (jamais la valeur) d'une variable d'environnement portant une cle SerpApi.
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Cle utilisee quand le fichier ne precise pas `api_key_env` (comportement historique).
DEFAULT_API_KEY_ENV = "SEARCH_SERPAPI_KEY"


@dataclass(frozen=True)
class TripWatchConfig:
    origin: str
    destination: str
    destination_name: str
    currency: str
    schedule: str
    timezone: str  # nom IANA (ex: "Europe/Oslo", ou "UTC") — meme mecanisme que flightdeals.config
    departure_center: date
    departure_tolerance_days: int
    duration_center_days: int
    duration_tolerance_days: int
    required_stops: int   # nombre d'escales EXACT exige (demande d'une amie : ni direct ni 2+)
    travel_class: int     # SerpApi: 1=Economy 2=Premium Economy 3=Affaires 4=Premiere
    api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    api_key_env: str = DEFAULT_API_KEY_ENV  # NOM de la variable d'environnement de la cle (jamais la valeur)
    watch_id: str = ""      # nom du fichier sans .yaml (ex: "santiago")
    enabled: bool = True    # false = alerte en pause (aucune recherche, aucun cout)
    db_file: str = ""       # fichier SQLite de l'alerte, dans le dossier de donnees


DB_FILE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.db$")


def _default_config_path() -> Path:
    return Path(os.environ.get("TRIP_WATCH_CONFIG_PATH", "watches/santiago.yaml"))


def load_config(path: "Path | str | None" = None, *, require_secrets: bool = True) -> TripWatchConfig:
    """`require_secrets=False` : valide uniquement le fichier (utilise par l'interface
    d'edition, qui n'a volontairement aucun secret) ; les champs secrets valent alors ""."""
    config_path = Path(path) if path else _default_config_path()
    if not config_path.exists():
        raise ConfigError(f"Fichier de config introuvable: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML illisible dans {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} doit contenir des cles/valeurs YAML")

    # `api_key_env` (optionnel) : NOM de la variable d'environnement qui porte la cle SerpApi
    # de cette veille — permet a chaque veille d'avoir sa propre cle/son propre quota. Si
    # precise, cette variable est exigee (pas de repli silencieux sur une autre cle, qui
    # consommerait le quota d'une autre veille). Absent : comportement historique,
    # SEARCH_SERPAPI_KEY prioritaire sur SERPAPI_KEY (cle du produit principal, en secours).
    api_key_env = raw.get("api_key_env")
    if api_key_env is not None and (not isinstance(api_key_env, str) or not ENV_NAME_RE.match(api_key_env)):
        raise ConfigError(f"api_key_env doit etre un nom de variable (ex: SERPAPI_KEY_3), recu: {api_key_env!r}")

    if api_key_env:
        api_key = os.environ.get(api_key_env, "").strip()
        missing_key_msg = f"{api_key_env} (api_key_env) manquant dans l'environnement"
    else:
        api_key = (os.environ.get(DEFAULT_API_KEY_ENV) or os.environ.get("SERPAPI_KEY", "")).strip()
        missing_key_msg = "SEARCH_SERPAPI_KEY (ou SERPAPI_KEY a defaut) manquant dans l'environnement"
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if require_secrets:
        if not api_key:
            raise ConfigError(missing_key_msg)
        if not bot_token or not chat_id:
            raise ConfigError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquant dans l'environnement")

    timezone = raw.get("timezone", "UTC")  # defaut UTC si absent (retro-compatible)
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"timezone doit etre un nom de fuseau IANA valide (ex: 'Europe/Oslo', 'UTC'), recu: {timezone!r}"
        ) from exc

    filters = raw.get("filters", {})
    required_stops = filters.get("required_stops", 1)
    travel_class = filters.get("travel_class", 2)

    if required_stops < 0:
        raise ConfigError(f"filters.required_stops doit etre >= 0, recu: {required_stops}")
    if travel_class not in (1, 2, 3, 4):
        raise ConfigError(f"filters.travel_class doit etre 1/2/3/4 (Economy/Premium/Affaires/Premiere), recu: {travel_class}")

    try:
        config = TripWatchConfig(
            origin=raw["origin"],
            destination=raw["destination"],
            destination_name=raw["destination_name"],
            currency=raw["currency"],
            schedule=raw["schedule"],
            timezone=timezone,
            departure_center=date.fromisoformat(raw["departure"]["center"]),
            departure_tolerance_days=raw["departure"]["tolerance_days"],
            duration_center_days=raw["duration"]["center_days"],
            duration_tolerance_days=raw["duration"]["tolerance_days"],
            required_stops=required_stops,
            travel_class=travel_class,
            api_key=api_key,
            telegram_bot_token=bot_token,
            telegram_chat_id=chat_id,
            api_key_env=api_key_env or DEFAULT_API_KEY_ENV,
            watch_id=config_path.stem,
            enabled=raw.get("enabled", True),
            # Une base par alerte : les historiques ne se melangent jamais. Santiago garde
            # explicitement sa base historique (db_file: trip_watch.db dans son fichier).
            db_file=raw.get("db_file") or f"trip_watch_{config_path.stem}.db",
        )
    except KeyError as exc:
        raise ConfigError(f"Cle de config manquante dans {config_path}: {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Valeur invalide dans {config_path}: {exc}") from exc

    _validate(config)
    return config


def _validate(config: TripWatchConfig) -> None:
    """Garde-fous ajoutes avec l'interface d'edition : une saisie incoherente doit etre
    refusee a l'enregistrement plutot que de faire echouer le run de 10:30 en silence."""
    for field_name in ("origin", "destination"):
        value = getattr(config, field_name)
        if not isinstance(value, str) or not _IATA_RE.match(value):
            raise ConfigError(f"{field_name} doit etre un code IATA de 3 lettres majuscules (ex: OSL), recu: {value!r}")
    if not isinstance(config.schedule, str) or not _HHMM_RE.match(config.schedule):
        raise ConfigError(f"schedule doit etre au format 'HH:MM' (24h), recu: {config.schedule!r}")
    if not isinstance(config.destination_name, str) or not config.destination_name.strip():
        raise ConfigError("destination_name ne doit pas etre vide")
    if not isinstance(config.currency, str) or not re.match(r"^[A-Z]{3}$", config.currency):
        raise ConfigError(f"currency doit etre un code devise de 3 lettres (ex: NOK), recu: {config.currency!r}")
    for field_name in ("departure_tolerance_days", "duration_tolerance_days", "duration_center_days", "required_stops"):
        value = getattr(config, field_name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"{field_name} doit etre un entier >= 0, recu: {value!r}")
    if not isinstance(config.enabled, bool):
        raise ConfigError(f"enabled doit valoir true ou false, recu: {config.enabled!r}")
    if not isinstance(config.db_file, str) or not DB_FILE_RE.match(config.db_file):
        raise ConfigError(f"db_file doit etre un simple nom de fichier .db (ex: trip_watch_lima.db), recu: {config.db_file!r}")
    if config.duration_center_days - config.duration_tolerance_days < 1:
        raise ConfigError("duration: center_days - tolerance_days doit valoir au moins 1 jour")
