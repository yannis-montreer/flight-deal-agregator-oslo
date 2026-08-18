"""Notification Telegram (spec section 13) : appel HTTP direct a l'API Telegram
(sendMessage), pas le SDK python-telegram-bot — pour rester sur des dependances minimales
(voir plan / stack technique : requests/httpx suffisent pour un envoi a sens unique).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

_FRENCH_MONTHS = [
    "", "janvier", "fevrier", "mars", "avril", "mai", "juin",
    "juillet", "aout", "septembre", "octobre", "novembre", "decembre",
]


class TelegramError(Exception):
    """Envoi Telegram echoue : reseau, token invalide, chat introuvable, ok:false, etc."""


@dataclass(frozen=True)
class DealMessage:
    """Tout ce qu'il faut pour rendre le message d'un deal (spec section 13 : origine,
    destination, prix, devise, dates, compagnie si disponible, escales, discount, score,
    lien). destination_name est le nom affiche (ex: 'Tokyo'), destination le code IATA."""

    origin: str
    destination: str
    destination_name: Optional[str]
    price: float
    currency: str
    departure_date: str  # YYYY-MM-DD
    return_date: Optional[str]  # YYYY-MM-DD, None si one-way
    airline: Optional[str]
    stops: Optional[int]
    duration_minutes: Optional[int]  # duree totale de vol (escales comprises), pas le sejour
    discount: float  # fraction, ex 0.40 pour -40%
    score: int
    source_url: Optional[str]


def render_message(deal: DealMessage) -> str:
    dest_label = f"{deal.destination_name} ({deal.destination})" if deal.destination_name else deal.destination
    price_label = f"{_format_price(deal.price)} {deal.currency}" + (" A/R" if deal.return_date else "")
    discount_label = f"-{round(deal.discount * 100)}% vs historique"
    dates_label = _format_dates(deal.departure_date, deal.return_date)

    lines = [
        "\U0001F525 FLIGHT DEAL",
        f"{deal.origin} → {dest_label}",
        price_label,
        discount_label,
        dates_label,
    ]
    if deal.airline:
        lines.append(deal.airline)
    lines.append(_format_stops(deal.stops))
    duration_label = _format_duration(deal.duration_minutes)
    if duration_label:
        lines.append(duration_label)
    lines.append(f"Deal score: {deal.score}/100")
    if deal.source_url:
        lines.append("Voir le vol :")
        lines.append(deal.source_url)

    return "\n".join(lines)


def _format_price(price: float) -> str:
    return f"{round(price):,}".replace(",", " ")  # separateur milliers = espace (convention NOK/FR)


def _format_dates(departure_date: str, return_date: Optional[str]) -> str:
    dep = date.fromisoformat(departure_date)
    if not return_date:
        return f"{dep.day} {_FRENCH_MONTHS[dep.month]}"

    ret = date.fromisoformat(return_date)
    if dep.month == ret.month and dep.year == ret.year:
        return f"{dep.day} → {ret.day} {_FRENCH_MONTHS[dep.month]}"
    return f"{dep.day} {_FRENCH_MONTHS[dep.month]} → {ret.day} {_FRENCH_MONTHS[ret.month]}"


def _format_duration(duration_minutes: Optional[int]) -> Optional[str]:
    """None si duree inconnue -> la ligne est simplement omise (voir render_message), pas
    affichee comme "inconnue" (contrairement aux escales) : moins critique a signaler."""
    if duration_minutes is None:
        return None
    hours, minutes = divmod(duration_minutes, 60)
    return f"Duree totale: {hours}h{minutes:02d}" if minutes else f"Duree totale: {hours}h"


def _format_stops(stops: Optional[int]) -> str:
    if stops is None:
        return "Escales inconnues"
    if stops == 0:
        return "Vol direct"
    if stops == 1:
        return "1 escale"
    return f"{stops} escales"


def send_message(bot_token: str, chat_id: str, text: str, *, timeout_seconds: float = 15.0) -> None:
    """Leve TelegramError si l'envoi echoue. N'ecrit RIEN en base — c'est a l'appelant
    (pipeline.py, jalon M8) d'inserer dans notified_deals SEULEMENT apres un retour reussi
    de cette fonction (spec section 12 : un echec ne doit jamais etre marque "notifie",
    sinon un deal reel serait perdu silencieusement en cas de panne Telegram)."""
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    try:
        response = httpx.post(url, data={"chat_id": chat_id, "text": text}, timeout=timeout_seconds)
    except httpx.HTTPError as exc:
        raise TelegramError(f"Envoi Telegram impossible (reseau): {exc}") from exc

    if response.status_code != 200:
        raise TelegramError(f"Telegram a renvoye HTTP {response.status_code}: {response.text[:300]}")

    data = response.json()
    if not data.get("ok"):
        raise TelegramError(f"Telegram a renvoye ok=false: {data}")


def send_deal_notification(
    bot_token: str, chat_id: str, deal: DealMessage, *, timeout_seconds: float = 15.0
) -> None:
    text = render_message(deal)
    send_message(bot_token, chat_id, text, timeout_seconds=timeout_seconds)
    logger.info(
        "Notification Telegram envoyee: %s -> %s, %s %s", deal.origin, deal.destination, deal.price, deal.currency
    )


def throttled_send(
    bot_token: str,
    chat_id: str,
    deals: list[DealMessage],
    *,
    send_delay_seconds: float,
    timeout_seconds: float = 15.0,
) -> list[DealMessage]:
    """Envoie plusieurs deals avec un throttle entre chaque envoi (evite de flooder Telegram
    si beaucoup de destinations qualifient le meme jour). Retourne uniquement les deals dont
    l'envoi a REUSSI — pipeline.py (jalon M8) n'insere notified_deals que pour ceux-la. Un
    echec sur un deal est logue et n'interrompt jamais les envois suivants."""
    sent: list[DealMessage] = []
    for index, deal in enumerate(deals):
        try:
            send_deal_notification(bot_token, chat_id, deal, timeout_seconds=timeout_seconds)
            sent.append(deal)
        except TelegramError:
            logger.exception(
                "Echec d'envoi Telegram pour %s -> %s (ignore, le run continue)", deal.origin, deal.destination
            )
        if index < len(deals) - 1:
            time.sleep(send_delay_seconds)
    return sent
