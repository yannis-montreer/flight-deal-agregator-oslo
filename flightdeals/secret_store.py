"""Secrets ajoutes depuis l'interface : bots Telegram des amis et cles SerpApi supplementaires.

Fichier `secrets/store.json` (jamais commite, jamais dans une image Docker) :

    {"serpapi_keys": {"SERPAPI_KEY_ANNA": "<cle>"},
     "bots": {"anna": {"alias": "Anna", "token": "<token>", "chat_id": "123", "username": "x_bot"}}}

- L'interface l'ecrit (ecriture atomique) ; les deux services le lisent en lecture seule, a
  chaque relecture des veilles (toutes les 5 minutes) : rien a redemarrer.
- Le bot principal ("main") reste celui de .env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) : il
  recoit en plus tous les messages techniques (recap, budget, erreurs).
- Les cles de .env restent prioritaires sur celles de l'interface a nom egal.
- Aucune valeur n'est jamais journalisee ni renvoyee a l'interface (seulement les 4 derniers
  caracteres d'un token pour le reconnaitre).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

STORE_FILENAME = "store.json"
MAIN_BOT_ID = "main"
MAIN_BOT_ALIAS = "Principal (moi)"
BOT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
CHAT_ID_RE = re.compile(r"^-?\d{1,20}$|^@[A-Za-z0-9_]{5,32}$")
TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,60}$")


class StoreError(Exception):
    """Fichier de secrets illisible ou valeur invalide."""


@dataclass(frozen=True)
class Bot:
    bot_id: str
    alias: str
    token: str
    chat_id: str

    def __repr__(self) -> str:  # jamais le token dans un log ou une trace
        return f"Bot({self.bot_id!r}, {self.alias!r})"


def store_dir() -> Path:
    return Path(os.environ.get("FLIGHTDEALS_SECRETS_DIR", "secrets"))


def read_store(directory: Optional[Path] = None) -> dict[str, Any]:
    path = (directory or store_dir()) / STORE_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"serpapi_keys": {}, "bots": {}}
    except OSError as exc:
        raise StoreError(f"{STORE_FILENAME} illisible ({type(exc).__name__})") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise StoreError(f"{STORE_FILENAME} n'est pas du JSON valide") from exc
    if not isinstance(data, dict):
        raise StoreError(f"{STORE_FILENAME} doit contenir un objet JSON")
    keys = data.get("serpapi_keys") or {}
    bots = data.get("bots") or {}
    if not isinstance(keys, dict) or not isinstance(bots, dict):
        raise StoreError(f"{STORE_FILENAME} : serpapi_keys et bots doivent etre des objets")
    return {"serpapi_keys": keys, "bots": bots}


def write_store(directory: Path, data: Mapping[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{STORE_FILENAME}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, directory / STORE_FILENAME)


def resolve_key(name: str, *, environ: Mapping[str, str] = os.environ, directory: Optional[Path] = None) -> str:
    """Valeur de la cle SerpApi `name` : .env d'abord, puis l'interface. "" si inconnue."""
    value = (environ.get(name) or "").strip()
    if value:
        return value
    try:
        stored = read_store(directory)["serpapi_keys"].get(name)
    except StoreError:
        return ""
    return stored.strip() if isinstance(stored, str) else ""


def stored_key_names(directory: Optional[Path] = None) -> list[str]:
    try:
        keys = read_store(directory)["serpapi_keys"]
    except StoreError:
        return []
    return sorted(name for name, value in keys.items() if isinstance(value, str) and value.strip())


def main_bot(environ: Mapping[str, str] = os.environ) -> Optional[Bot]:
    token = (environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (environ.get("TELEGRAM_CHAT_ID") or "").strip()
    return Bot(MAIN_BOT_ID, MAIN_BOT_ALIAS, token, chat_id) if token and chat_id else None


def stored_bots(directory: Optional[Path] = None) -> dict[str, Bot]:
    bots = {}
    for bot_id, entry in read_store(directory)["bots"].items():
        if not (isinstance(entry, dict) and BOT_ID_RE.match(bot_id) and bot_id != MAIN_BOT_ID):
            continue
        token, chat_id = str(entry.get("token") or "").strip(), str(entry.get("chat_id") or "").strip()
        if token and chat_id:
            bots[bot_id] = Bot(bot_id, str(entry.get("alias") or bot_id), token, chat_id)
    return bots


def parse_notify(raw: Any) -> tuple[str, ...]:
    """Champ `notify` d'une veille : liste d'identifiants de bots. Absent = bot principal
    seul (comportement historique)."""
    if raw is None:
        return (MAIN_BOT_ID,)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"notify doit etre une liste non vide d'identifiants de bots, recu: {raw!r}")
    ids: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not BOT_ID_RE.match(item):
            raise ValueError(f"notify : identifiant de bot invalide {item!r}")
        if item not in ids:
            ids.append(item)
    return tuple(ids)


def resolve_bots(ids: Iterable[str], *, environ: Mapping[str, str] = os.environ,
                 directory: Optional[Path] = None) -> tuple[Bot, ...]:
    """Bots destinataires d'une veille. Leve StoreError si l'un d'eux est inconnu (la veille
    est alors ignoree et l'erreur journalisee, comme une cle manquante)."""
    ids = list(ids)
    known: dict[str, Bot] = {}
    main = main_bot(environ)
    if main:
        known[MAIN_BOT_ID] = main
    if any(i != MAIN_BOT_ID for i in ids):
        known.update(stored_bots(directory))
    missing = [i for i in ids if i not in known]
    if missing:
        raise StoreError(f"bot(s) Telegram inconnu(s) : {', '.join(missing)}")
    return tuple(known[i] for i in ids)


def mask(token: str) -> str:
    return f"••••{token[-4:]}" if len(token) >= 8 else "••••"
