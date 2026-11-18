"""Appels Telegram faits par l'interface, toujours a la demande explicite de l'utilisateur :
verifier un token (getMe), trouver le chat_id d'un ami (getUpdates), envoyer un message de test.

Jamais le token dans un message d'erreur : seulement le type d'exception ou la description
renvoyee par Telegram (ex. "Unauthorized")."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from webui.i18n import Msg

API = "https://api.telegram.org"


class TelegramToolError(Exception):
    pass


@dataclass(frozen=True)
class Chat:
    chat_id: str
    label: str


def _call(token: str, method: str, data: dict | None = None, timeout: float = 10.0) -> object:
    try:
        response = httpx.post(f"{API}/bot{token}/{method}", data=data or {}, timeout=timeout)
    except httpx.HTTPError as exc:
        raise TelegramToolError(Msg("Telegram injoignable ({error})", error=type(exc).__name__)) from None
    try:
        payload = response.json()
    except ValueError:
        raise TelegramToolError(Msg("Réponse Telegram illisible (HTTP {status})", status=response.status_code)) from None
    if not isinstance(payload, dict) or not payload.get("ok"):
        description = str(payload.get("description", "")) if isinstance(payload, dict) else ""
        if response.status_code == 401:
            raise TelegramToolError(Msg("Token refusé par Telegram : vérifie qu'il est copié en entier depuis @BotFather."))
        raise TelegramToolError(Msg("Telegram a refusé la demande : {reason}", reason=description[:200] or str(response.status_code)))
    return payload.get("result")


def bot_username(token: str) -> str:
    result = _call(token, "getMe")
    return str(result.get("username", "")) if isinstance(result, dict) else ""


def find_chats(token: str) -> list[Chat]:
    """Conversations recentes du bot (les messages des dernieres 24 h environ, que Telegram
    garde tant que personne ne les a lus)."""
    result = _call(token, "getUpdates", {"timeout": 0, "limit": 100})
    chats: dict[str, Chat] = {}
    for update in result if isinstance(result, list) else []:
        if not isinstance(update, dict):
            continue
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat") if isinstance(update.get(key), dict) else None
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            name = " ".join(str(chat[k]) for k in ("first_name", "last_name") if chat.get(k)) or str(chat.get("title", ""))
            if chat.get("username"):
                name = f"{name} (@{chat['username']})".strip()
            chats[str(chat["id"])] = Chat(str(chat["id"]), name or str(chat["id"]))
    return list(chats.values())


def send_test(token: str, chat_id: str, alias: str) -> None:
    _call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": f"✅ Test : le bot « {alias} » est bien relié aux alertes de vols. Tu recevras ici les alertes choisies pour toi.",
    })
