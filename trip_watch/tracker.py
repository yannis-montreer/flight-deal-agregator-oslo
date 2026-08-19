"""Logique du suivi quotidien : construit la tranche du jour (dates de depart completes x
UNE duree en rotation), interroge SerpApi via google_flights (point-a-point, comme valide
au Spike), stocke chaque observation, envoie un digest quotidien Telegram avec le meilleur
prix trouve — signale en plus si c'est un nouveau minimum jamais vu pour cette combinaison
exacte (destination + dates)."""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from flightdeals.collectors.serpapi_client import QuotaExceededError, SerpApiClient, SerpApiError
from flightdeals.notify.telegram import TelegramError, send_message

from trip_watch.config import TripWatchConfig
from trip_watch.db import get_connection, get_historical_min, insert_observation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailyResult:
    departure_date: str
    return_date: str
    price: Optional[float]
    currency: str
    airline: Optional[str]
    stops: Optional[int]
    price_level: Optional[str]
    error: Optional[str]
    search_url: Optional[str]        # lien Google Flights (meme recherche, dates prefiltrees)
    typical_avg_price: Optional[float]  # moyenne de price_insights.price_history (~60j), pas juste la fourchette basse/haute


def departure_dates(center: date, tolerance_days: int) -> list[date]:
    """Toutes les dates de depart de la plage — testees en ENTIER chaque jour (axe le plus
    susceptible de faire varier le prix jour a jour, contrairement a la duree du sejour)."""
    return [center + timedelta(days=d) for d in range(-tolerance_days, tolerance_days + 1)]


def today_duration(center_days: int, tolerance_days: int, today: date) -> int:
    """Rotation deterministe et SANS ETAT : quelle duree de sejour tester aujourd'hui.
    `today.toordinal()` avance de 1 chaque jour civil -> le modulo cycle naturellement sur
    toutes les valeurs de la plage tous les (2*tolerance_days + 1) jours, sans avoir besoin
    de retenir "quelle etait la derniere duree testee" en base (resistant a un redemarrage
    du conteneur a n'importe quel moment, meme logique que scheduler.seconds_until_next_run)."""
    durations = list(range(center_days - tolerance_days, center_days + tolerance_days + 1))
    return durations[today.toordinal() % len(durations)]


def _search_one(
    client: SerpApiClient, *, origin: str, destination: str, outbound_date: date, return_date: date, currency: str
) -> DailyResult:
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": outbound_date.isoformat(),
        "return_date": return_date.isoformat(),
        "type": "1",  # round trip selon la doc SerpApi au moment de l'ecriture
        "currency": currency,
    }

    def _error(message: str, price_level: Optional[str] = None, search_url: Optional[str] = None) -> DailyResult:
        return DailyResult(
            departure_date=outbound_date.isoformat(), return_date=return_date.isoformat(),
            price=None, currency=currency, airline=None, stops=None, price_level=price_level, error=message,
            search_url=search_url, typical_avg_price=None,
        )

    try:
        data = client.search(params)
    except (QuotaExceededError, SerpApiError) as exc:
        return _error(str(exc))

    # Lien vers la recherche (memes origine/destination/dates) sur Google Flights — PAS un
    # lien de reservation d'un vol precis (l'API n'en expose pas), mais le plus proche
    # equivalent disponible : cliquer dessus rouvre les memes resultats en direct.
    search_url = (data.get("search_metadata") or {}).get("google_flights_url")
    insights = data.get("price_insights") or {}
    candidates = data.get("best_flights") or data.get("other_flights") or []
    if not candidates:
        return _error(
            data.get("error") or "aucun vol trouve", price_level=insights.get("price_level"), search_url=search_url
        )

    best = candidates[0]
    legs = best.get("flights", [])
    airlines = ", ".join(sorted({leg.get("airline", "?") for leg in legs})) if legs else None

    # Moyenne reelle sur l'historique fourni par SerpApi (price_history, ~60j de points
    # quotidiens), plus fin que typical_price_range qui n'est qu'une fourchette [bas, haut].
    history = insights.get("price_history") or []
    typical_prices = [
        entry[1] for entry in history
        if isinstance(entry, (list, tuple)) and len(entry) == 2
        and isinstance(entry[1], (int, float)) and not isinstance(entry[1], bool)
    ]
    typical_avg_price = statistics.mean(typical_prices) if typical_prices else None

    return DailyResult(
        departure_date=outbound_date.isoformat(),
        return_date=return_date.isoformat(),
        price=best.get("price"),
        currency=currency,
        airline=airlines,
        stops=max(len(legs) - 1, 0) if legs else None,
        price_level=insights.get("price_level"),
        error=None,
        search_url=search_url,
        typical_avg_price=typical_avg_price,
    )


def run_daily_check(config: TripWatchConfig, db_path: "Path | str") -> None:
    now = datetime.now(timezone.utc)
    duration_days = today_duration(config.duration_center_days, config.duration_tolerance_days, now.date())
    dep_dates = departure_dates(config.departure_center, config.departure_tolerance_days)

    logger.info(
        "Verification quotidienne: %s, duree=%dj, %d dates de depart testees",
        config.destination, duration_days, len(dep_dates),
    )

    conn = get_connection(db_path)
    try:
        evaluated: list[tuple[DailyResult, bool]] = []
        remaining_searches: Optional[int] = None
        with SerpApiClient(api_key=config.api_key) as client:
            # account.json est gratuit (ne consomme pas de quota) — utilise ici uniquement
            # pour AFFICHER le budget reel du compte (demande utilisateur : visibilite sur le
            # quota d'un compte tiers dont on n'a pas le dashboard), pas pour bloquer le run
            # (trip_watch n'a pas de garde-fou budget comme flightdeals.pipeline, le volume
            # est deja tres faible ~150/mois par design).
            try:
                account = client.get_account_info()
                remaining_searches = account.get("total_searches_left")
            except Exception:
                logger.exception("Impossible de recuperer le quota SerpApi (account.json) - omis du digest")

            for dep in dep_dates:
                ret = dep + timedelta(days=duration_days)
                result = _search_one(
                    client, origin=config.origin, destination=config.destination,
                    outbound_date=dep, return_date=ret, currency=config.currency,
                )

                obs_id = insert_observation(
                    conn,
                    observed_at=now.isoformat(),
                    destination=config.destination,
                    departure_date=result.departure_date,
                    return_date=result.return_date,
                    price=result.price,
                    currency=result.currency,
                    airline=result.airline,
                    stops=result.stops,
                    price_level=result.price_level,
                    error=result.error,
                )

                is_new_min = False
                if result.price is not None:
                    prior_min = get_historical_min(
                        conn, config.destination, result.departure_date, result.return_date, exclude_id=obs_id
                    )
                    is_new_min = prior_min is None or result.price < prior_min

                evaluated.append((result, is_new_min))
                logger.info(
                    "  %s -> %s: %s %s%s",
                    result.departure_date, result.return_date,
                    result.price if result.price is not None else f"ERREUR: {result.error}",
                    result.currency, " [NOUVEAU MIN]" if is_new_min else "",
                )
    finally:
        conn.close()

    valid = [(r, is_min) for r, is_min in evaluated if r.price is not None]
    if not valid:
        logger.warning("Aucun prix exploitable aujourd'hui pour %s (duree=%dj)", config.destination, duration_days)
        return

    best_result, best_is_new_min = min(valid, key=lambda pair: pair[0].price)
    _send_daily_digest(config, best_result, best_is_new_min, duration_days, remaining_searches)


def _format_date_fr(iso_date: str) -> str:
    return date.fromisoformat(iso_date).strftime("%d/%m/%Y")


def _format_price(price: float) -> str:
    return f"{round(price):,}".replace(",", " ")


def _format_daily_digest(
    config: TripWatchConfig, result: DailyResult, is_new_min: bool, duration_days: int,
    remaining_searches: Optional[int],
) -> str:
    lines = [
        f"\U0001F30E Suivi {config.destination_name} — sejour de {duration_days}j",
        f"Meilleur prix du jour : {_format_date_fr(result.departure_date)} → {_format_date_fr(result.return_date)}",
        f"{_format_price(result.price)} {result.currency}",
    ]
    if result.airline:
        lines.append(result.airline)
    if result.stops is not None:
        lines.append("Vol direct" if result.stops == 0 else f"{result.stops} escale(s)")
    if result.price_level:
        eval_line = f"Evaluation Google : prix {result.price_level}"
        if result.typical_avg_price is not None:
            eval_line += f" (moyenne habituelle ~{_format_price(result.typical_avg_price)} {result.currency})"
        lines.append(eval_line)
    if remaining_searches is not None:
        # Visibilite sur le quota du compte SerpApi utilise (potentiellement pas le notre —
        # demande utilisateur : pas d'acces au dashboard du compte tiers fournissant la cle).
        lines.append(f"Quota SerpApi restant : {remaining_searches} recherches")
    if result.search_url:
        lines.append("Voir sur Google Flights :")
        lines.append(result.search_url)
    if is_new_min:
        lines.insert(0, "\U0001F525 NOUVEAU MINIMUM pour cette combinaison exacte de dates !")

    return "\n".join(lines)


def _send_daily_digest(
    config: TripWatchConfig, result: DailyResult, is_new_min: bool, duration_days: int,
    remaining_searches: Optional[int],
) -> None:
    text = _format_daily_digest(config, result, is_new_min, duration_days, remaining_searches)
    try:
        send_message(config.telegram_bot_token, config.telegram_chat_id, text)
        logger.info("Digest quotidien envoye")
    except TelegramError:
        logger.exception("Echec d'envoi du digest quotidien (ignore, on reessaiera demain)")
