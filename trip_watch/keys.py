"""Registre des cles SerpApi disponibles, publie pour l'interface d'edition SANS aucun secret.

Le service des alertes est le seul a connaitre les valeurs des cles (via .env). Il publie
dans `keys.json` uniquement : le NOM de chaque variable de cle presente dans son
environnement, et les compteurs de son compte SerpApi (account.json, appel GRATUIT qui ne
consomme aucune recherche). Jamais la valeur de la cle, ni l'e-mail du compte, ni un message
d'erreur brut (une URL d'erreur httpx contiendrait la cle en parametre).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from flightdeals import secret_store

logger = logging.getLogger(__name__)

KEY_NAME_RE = re.compile(r"^[A-Z0-9_]*SERPAPI_KEY[A-Z0-9_]*$")
REGISTRY_FILENAME = "keys.json"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Republie au moins toutes les heures : flightdeals (autre service) consomme la cle
# SERPAPI_KEY a 10:30 sans prevenir trip-watch ; account.json etant gratuit, on rafraichit.
REFRESH_SECONDS = 3600


def available_key_names(environ: Mapping[str, str]) -> list[str]:
    return sorted(name for name, value in environ.items() if KEY_NAME_RE.match(name) and value.strip())


def available_keys(environ: Mapping[str, str], stored: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """NOM -> (valeur, origine "env" ou "interface"). A nom egal, .env l'emporte."""
    keys = {name: (value.strip(), "interface") for name, value in stored.items()
            if isinstance(value, str) and value.strip() and KEY_NAME_RE.match(name)}
    keys.update({name: (environ[name].strip(), "env") for name in available_key_names(environ)})
    return dict(sorted(keys.items()))


def stored_keys(directory: Optional[Path] = None) -> dict[str, str]:
    try:
        return dict(secret_store.read_store(directory)["serpapi_keys"])
    except secret_store.StoreError:
        logger.warning("Fichier de secrets de l'interface illisible : cles ajoutees depuis l'interface ignorees")
        return {}


def _as_int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def build_registry(environ: Mapping[str, str], fetch_account: Callable[[str], dict],
                   stored: Optional[Mapping[str, str]] = None) -> dict:
    keys = []
    for name, (value, source) in available_keys(environ, stored or {}).items():
        entry: dict[str, Any] = {
            "name": name, "source": source, "searches_per_month": None, "searches_left": None,
            "used_this_period": None, "renewal_date": None, "error": None,
        }
        try:
            account = fetch_account(value)
            entry["searches_per_month"] = _as_int(account.get("searches_per_month"))
            entry["searches_left"] = _as_int(account.get("total_searches_left"))
            entry["used_this_period"] = _as_int(account.get("this_month_usage"))
            renewal = account.get("plan_renewal_date")
            # Seulement une date AAAA-MM-JJ (jamais un texte libre venu de l'API).
            entry["renewal_date"] = renewal if isinstance(renewal, str) and _DATE_RE.match(renewal) else None
        except Exception as exc:  # noqa: BLE001 - un compte injoignable ne doit rien bloquer
            entry["error"] = type(exc).__name__  # jamais str(exc) : peut contenir la cle
        keys.append(entry)
    return {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "keys": keys}


def write_registry(status_dir: Path, registry: dict) -> None:
    status_dir.mkdir(parents=True, exist_ok=True)
    target = status_dir / REGISTRY_FILENAME
    tmp = status_dir / f".{REGISTRY_FILENAME}.tmp"
    tmp.write_text(json.dumps(registry, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)


def publish(status_dir: Optional[Path], fetch_account: Callable[[str], dict], *, quiet: bool = False) -> None:
    """Ne leve jamais : le registre n'est qu'une aide a l'affichage. `quiet` : rafraichissement
    periodique, sans ligne de log (evite 24 lignes par jour)."""
    if status_dir is None:
        return
    try:
        registry = build_registry(os.environ, fetch_account, stored_keys())
        write_registry(status_dir, registry)
        (logger.debug if quiet else logger.info)("Registre des cles publie : %s", ", ".join(k["name"] for k in registry["keys"]) or "aucune")
    except Exception:  # noqa: BLE001
        logger.warning("Impossible de publier le registre des cles (ignore)")
