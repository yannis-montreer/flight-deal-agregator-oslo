"""Orchestration d'un cycle quotidien complet (spec section 5) :

    Collecte SerpApi -> Parsing/normalisation -> Stockage SQLite -> Calcul des deals
    -> Dedoublonnage -> Notifications Telegram

run_once() est LA fonction appelee par le scheduler (jalon M9). Principe directeur (spec
section 14) : une erreur sur UNE destination/observation ne doit jamais arreter tout le run.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from flightdeals.analysis.bucketing import (
    compute_stops_bucket,
    compute_travel_period_bucket,
    compute_trip_length_nights,
)
from flightdeals.analysis.scoring import check_trip_length, evaluate_deal
from flightdeals.analysis.statistics import compute_price_stats
from flightdeals.collectors.flight_search import RawObservation, fetch_all_explore_destinations
from flightdeals.collectors.serpapi_client import QuotaExceededError, SerpApiClient, SerpApiError
from flightdeals.config import Config
from flightdeals.db.connection import get_connection
from flightdeals.db.repository import (
    FlightObservation,
    GoogleSignalNotification,
    NotifiedDeal,
    get_requests_used,
    increment_requests_used,
    insert_google_signal_notification,
    insert_notification,
    insert_observation,
    query_comparable_durations,
    query_comparable_prices,
)
from flightdeals.dedup import should_notify, should_notify_google_signal
from flightdeals.notify.telegram import (
    DealMessage,
    GoogleSignalMessage,
    TelegramError,
    send_deal_notification,
    send_google_signal_notification,
    send_message,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunSummary:
    """Retourne par run_once() — sert au log de fin de run et aux tests d'integration."""

    skipped_budget: bool
    observations_collected: int
    observations_stored: int
    deals_triggered: int
    deals_notified: int
    google_signals_checked: int = 0
    google_signals_sent: int = 0


@dataclass(frozen=True)
class ColdStartCandidate:
    """Observation exclue UNIQUEMENT pour "insufficient_history" (voir scoring.py) et dont la
    duree de sejour respecte quand meme deal.min/max_trip_length_nights — candidate a la
    verification complementaire aupres de Google (voir _check_cold_start_signals). Champs
    identiques a DealMessage moins discount/score, qui n'ont pas de sens sans historique."""

    obs_id: int
    origin: str
    destination: str
    destination_name: Optional[str]
    price: float
    currency: str
    departure_date: str
    return_date: Optional[str]
    airline: Optional[str]
    stops: Optional[int]
    duration_minutes: Optional[int]
    source_url: Optional[str]


def run_once(config: Config, db_path: "Path | str") -> RunSummary:
    run_started_at = datetime.now(timezone.utc)
    run_started_at_iso = run_started_at.isoformat()
    window_start_iso = (run_started_at - timedelta(days=config.deal.history_window_days)).isoformat()
    month_key = run_started_at.strftime("%Y-%m")

    conn = get_connection(db_path)
    try:
        with SerpApiClient(api_key=config.secrets.serpapi_key) as client:
            if _budget_exhausted(client, conn, config, month_key):
                summary = RunSummary(
                    skipped_budget=True,
                    observations_collected=0,
                    observations_stored=0,
                    deals_triggered=0,
                    deals_notified=0,
                )
                _send_daily_summary(config, conn, summary, month_key)
                return summary

            raw_observations = fetch_all_explore_destinations(
                client,
                origin=config.origin,
                currency=config.currency,
                travel_durations=config.serpapi.travel_durations,
                arrival_area_id=config.serpapi.arrival_area_id,
                month=config.serpapi.month,
            )
            # Approximation deliberee : compte 1 requete par duree TENTEE (pas par retry HTTP
            # interne). Simple, auditable, et surestime plutot que sous-estime les jours ou le
            # quota coupe la collecte en cours de route (la marge budget/200 est large, voir plan).
            increment_requests_used(conn, month_key, count=len(config.serpapi.travel_durations))

        stored_count = 0
        triggered_deals: list[tuple[int, DealMessage]] = []
        cold_start_candidates: list[ColdStartCandidate] = []

        for raw in raw_observations:
            try:
                outcome = _evaluate_one(conn, config, raw, run_started_at_iso, window_start_iso)
            except Exception:
                logger.exception("Echec de traitement d'une observation (ignoree, run continue): %r", raw)
                continue

            if outcome is None:
                continue  # non exploitable (pas de prix/destination/date) -> pas stockee

            stored_count += 1
            obs_id, deal_message, cold_start_candidate = outcome
            if deal_message is not None:
                triggered_deals.append((obs_id, deal_message))
            if cold_start_candidate is not None:
                cold_start_candidates.append(cold_start_candidate)

        notified_count = _dedup_and_notify(conn, config, triggered_deals, run_started_at)
        signals_checked, signals_sent = _check_cold_start_signals(
            conn, config, cold_start_candidates, run_started_at, month_key
        )

        logger.info(
            "Run termine: %d observations collectees, %d stockees, %d deals declenches, %d notifies, "
            "%d signaux Google verifies, %d envoyes",
            len(raw_observations),
            stored_count,
            len(triggered_deals),
            notified_count,
            signals_checked,
            signals_sent,
        )
        summary = RunSummary(
            skipped_budget=False,
            observations_collected=len(raw_observations),
            observations_stored=stored_count,
            deals_triggered=len(triggered_deals),
            deals_notified=notified_count,
            google_signals_checked=signals_checked,
            google_signals_sent=signals_sent,
        )
        _send_daily_summary(config, conn, summary, month_key)
        return summary
    finally:
        conn.close()


def _send_daily_summary(config: Config, conn, summary: RunSummary, month_key: str) -> None:
    """Recap quotidien envoye APRES chaque run, deal detecte ou non (demande explicite de
    l'utilisateur) : les seuils de detection sont volontairement stricts (spec section 10),
    donc le systeme peut rester silencieux plusieurs semaines sans ce recap — sans lui, rien
    ne distingue "aucun deal aujourd'hui" de "le systeme est en panne". Toggle :
    notification.daily_summary_enabled (peut devenir redondant une fois des deals reguliers)."""
    if not config.notification.telegram or not config.notification.daily_summary_enabled:
        return

    requests_used = get_requests_used(conn, month_key)

    if summary.skipped_budget:
        text = (
            "⚠️ Run du jour saute (budget SerpApi insuffisant)\n"
            f"{requests_used}/{config.serpapi.monthly_budget} requetes utilisees ce mois"
        )
    else:
        text = (
            "\U0001F4CA Recap quotidien — Flight Deal Aggregator OSL\n"
            f"{summary.observations_collected} destinations scannees, {summary.observations_stored} stockees\n"
            f"{summary.deals_triggered} deal(s) detecte(s) aujourd'hui\n"
            f"Budget SerpApi : {requests_used}/{config.serpapi.monthly_budget} requetes ce mois"
        )
        if summary.google_signals_checked > 0:
            # N'apparait que tant qu'il reste des buckets en cold-start (voir
            # _check_cold_start_signals) - disparait de lui-meme une fois l'historique mur.
            text += (
                f"\n\U0001F50D {summary.google_signals_sent} signal(aux) Google envoye(s) "
                f"({summary.google_signals_checked} verifie(s), cold-start)"
            )

    try:
        send_message(config.secrets.telegram_bot_token, config.secrets.telegram_chat_id, text)
        logger.info("Recap quotidien envoye")
    except TelegramError:
        logger.exception("Echec d'envoi du recap quotidien (ignore, pas bloquant pour le run)")


def _budget_exhausted(client: SerpApiClient, conn, config: Config, month_key: str) -> bool:
    """Deux signaux verifies independamment (n'importe lequel peut declencher le skip) :
    le compteur LOCAL contre notre cible auto-imposee (deal.serpapi.monthly_budget, 200 par
    defaut — plus stricte que le vrai quota, pour garder de la marge), et le budget REEL
    restant cote SerpApi (account.json, gratuit) en detection precoce si jamais le compteur
    local avait derive de la realite. Le compteur local reste utilisable seul si account.json
    est indisponible (spec: "compteur local en secours")."""
    local_used = get_requests_used(conn, month_key)
    local_remaining = config.serpapi.monthly_budget - local_used
    if local_remaining < config.serpapi.min_budget_reserve:
        logger.warning(
            "Budget local cible epuise: %d/%d requetes deja utilisees ce mois-ci - run saute",
            local_used,
            config.serpapi.monthly_budget,
        )
        return True

    try:
        account = client.get_account_info()
        real_remaining = account.get("total_searches_left")
    except Exception:
        logger.exception(
            "Impossible de verifier le budget reel via account.json (SerpApi indisponible ?), "
            "on se fie uniquement au compteur local pour cette verification"
        )
        return False

    if real_remaining is not None and real_remaining < config.serpapi.min_budget_reserve:
        logger.warning("Budget SerpApi reel presque epuise (%s requetes restantes) - run saute", real_remaining)
        return True

    return False


def _evaluate_one(
    conn,
    config: Config,
    raw: RawObservation,
    observed_at_iso: str,
    window_start_iso: str,
) -> Optional[tuple[int, Optional[DealMessage], Optional[ColdStartCandidate]]]:
    """None si l'observation n'est pas stockable (pas de prix/destination/date exploitable).
    Sinon (id_observation_stockee, DealMessage_ou_None, ColdStartCandidate_ou_None) — au plus
    un des deux derniers est non-None : DealMessage si la regle de declenchement du spec
    (section 10) est satisfaite, ColdStartCandidate si le SEUL motif d'exclusion est
    "insufficient_history" (et que la duree de sejour reste dans les bornes voulues) — voir
    _check_cold_start_signals. obs_id est TOUJOURS retourne des qu'une observation est stockee."""
    obs = _build_flight_observation(raw, observed_at_iso)
    if obs is None:
        return None

    # IMPORTANT anti-auto-comparaison : upper_bound = timestamp de DEBUT du run entier (le
    # meme pour toutes les observations traitees), jamais recalcule par observation — voir
    # repository.query_comparable_prices et le plan pour le detail du raisonnement.
    historical_prices = query_comparable_prices(
        conn,
        origin=obs.origin,
        destination=obs.destination,
        currency=obs.currency,
        travel_period_bucket=obs.travel_period_bucket,
        trip_length_nights=obs.trip_length_nights,
        duration_tolerance_nights=config.deal.duration_tolerance_nights,
        stops_bucket=obs.stops_bucket if obs.stops is not None else None,
        window_start=window_start_iso,
        upper_bound=observed_at_iso,
    )
    stats = compute_price_stats(historical_prices)

    # Meme logique anti-auto-comparaison que pour les prix (upper_bound = debut du run).
    # Scope volontairement plus large que les prix (pas de filtre periode/duree de sejour) :
    # voir repository.query_comparable_durations pour le raisonnement complet.
    historical_durations = query_comparable_durations(
        conn,
        origin=obs.origin,
        destination=obs.destination,
        stops_bucket=obs.stops_bucket if obs.stops is not None else None,
        window_start=window_start_iso,
        upper_bound=observed_at_iso,
    )

    # L'insert se fait AVANT l'evaluation mais APRES le calcul des stats : les stats ne
    # peuvent donc jamais inclure cette observation elle-meme (voir upper_bound ci-dessus),
    # et l'insert etant immediat/durable (autocommit, voir connection.py), l'observation
    # survit a un crash juste apres, meme si l'evaluation qui suit echoue. Si insert_observation
    # elle-meme leve (DB corrompue/verrouillee), l'exception propage a l'appelant (run_once) :
    # c'est une vraie panne, pas un cas a masquer.
    obs_id = insert_observation(conn, obs)

    try:
        evaluation = evaluate_deal(
            current_price=obs.price,
            historical_prices=historical_prices,
            stats=stats,
            stops_bucket=obs.stops_bucket,
            trip_length_nights=obs.trip_length_nights,
            current_duration_minutes=obs.duration_minutes,
            historical_durations=historical_durations,
            minimum_discount=config.deal.minimum_discount,
            minimum_observations=config.deal.minimum_observations,
            percentile_threshold=config.deal.percentile_threshold,
            discount_cap=config.scoring.discount_cap,
            confidence_saturation_count=config.scoring.confidence_saturation_count,
            max_duration_deviation_ratio=config.deal.max_duration_deviation_ratio,
            min_trip_length_nights=config.deal.min_trip_length_nights,
            max_trip_length_nights=config.deal.max_trip_length_nights,
            weights=config.scoring.weights,
            directness_bonus=config.scoring.directness_bonus,
        )
    except Exception:
        # L'observation est deja stockee de facon durable (ligne ci-dessus) : un bug de
        # scoring sur une donnee inattendue ne doit PAS faire perdre cette insertion, juste
        # etre traite comme "pas de deal detecte" pour cette observation precise.
        logger.exception(
            "Observation stockee (id=%d) mais echec du calcul de score - traitee comme "
            "'pas de deal' plutot que de faire perdre le stockage",
            obs_id,
        )
        return obs_id, None, None

    if not evaluation.triggers:
        if evaluation.exclusion_reason == "duration_deviation":
            # Cas notable a logger explicitement : toutes les conditions prix/historique
            # etaient reunies, seule la duree de vol a exclu ce deal — sinon invisible dans
            # les logs (contrairement aux autres raisons, courantes et peu interessantes).
            logger.info(
                "Deal exclu pour duree de vol anormale: %s -> %s, %s min (obs id=%d)",
                obs.origin, obs.destination, obs.duration_minutes, obs_id,
            )

        cold_start_candidate = None
        if evaluation.exclusion_reason == "insufficient_history" and check_trip_length(
            obs.trip_length_nights, config.deal.min_trip_length_nights, config.deal.max_trip_length_nights
        ):
            # Meme filtre de duree de sejour que evaluate_deal (voir check_trip_length) : pas
            # la peine d'alerter sur un sejour hors bornes juste parce qu'il n'a pas encore
            # d'historique — il serait de toute facon exclu une fois l'historique suffisant.
            cold_start_candidate = ColdStartCandidate(
                obs_id=obs_id, origin=obs.origin, destination=obs.destination,
                destination_name=obs.destination_name, price=obs.price, currency=obs.currency,
                departure_date=obs.departure_date, return_date=obs.return_date,
                airline=obs.airline, stops=obs.stops, duration_minutes=obs.duration_minutes,
                source_url=obs.source_url,
            )
        return obs_id, None, cold_start_candidate

    deal_message = DealMessage(
        origin=obs.origin,
        destination=obs.destination,
        destination_name=obs.destination_name,
        price=obs.price,
        currency=obs.currency,
        departure_date=obs.departure_date,
        return_date=obs.return_date,
        airline=obs.airline,
        stops=obs.stops,
        duration_minutes=obs.duration_minutes,
        discount=evaluation.discount,
        score=evaluation.score,
        source_url=obs.source_url,
    )
    return obs_id, deal_message, None


def _check_cold_start_signals(
    conn,
    config: Config,
    candidates: list[ColdStartCandidate],
    run_started_at: datetime,
    month_key: str,
) -> tuple[int, int]:
    """Palliatif au trou de detection en cold-start (demande utilisateur : "dans les 6 premiers
    jours il pourrait y avoir un deal qu'on rate") : interroge google_flights (le seul engine
    a exposer price_insights.price_level — google_travel_explore, utilise pour le scan
    quotidien, ne l'expose pas) sur les N candidats les moins chers du jour parmi ceux sans
    historique suffisant. Si Google qualifie le prix de "low", envoie un message Telegram
    DISTINCT (GoogleSignalMessage), jamais melange avec un vrai deal statistique (dedup
    separee, voir dedup.should_notify_google_signal / schema.sql).

    S'eteint tout seul par bucket des que evaluate_deal a assez d'historique (plus jamais
    exclusion_reason="insufficient_history" pour lui, voir pipeline._evaluate_one) : aucune
    logique d'extinction a gerer ici, `candidates` est deja vide pour un bucket mature.

    Retourne (nombre_verifie, nombre_envoye) pour le recap quotidien (RunSummary). Ne leve
    jamais : une erreur sur UN candidat est loguee et ignoree (spec section 14)."""
    if (
        not config.cold_start_check.enabled
        or not config.notification.telegram
        or not candidates
        or config.cold_start_check.max_candidates_per_run <= 0
    ):
        return 0, 0

    cheapest = sorted(candidates, key=lambda c: c.price)[: config.cold_start_check.max_candidates_per_run]
    checked = 0
    sent = 0

    try:
        with SerpApiClient(api_key=config.secrets.serpapi_key) as client:
            for candidate in cheapest:
                if candidate.return_date is None:
                    continue  # garde-fou theorique : check_trip_length exclut deja les one-way en amont

                try:
                    data = client.search({
                        "engine": "google_flights",
                        "departure_id": candidate.origin,
                        "arrival_id": candidate.destination,
                        "outbound_date": candidate.departure_date,
                        "return_date": candidate.return_date,
                        "type": "1",
                        "currency": candidate.currency,
                    })
                except QuotaExceededError:
                    checked += 1
                    logger.warning(
                        "Quota SerpApi atteint pendant la verification cold-start (%d/%d verifies), arret anticipe",
                        checked, len(cheapest),
                    )
                    break
                except SerpApiError:
                    checked += 1
                    logger.exception(
                        "Echec de verification Google (cold-start) pour %s -> %s (ignore)",
                        candidate.origin, candidate.destination,
                    )
                    continue

                checked += 1
                price_level = (data.get("price_insights") or {}).get("price_level")
                if price_level != "low":
                    continue

                try:
                    decision = should_notify_google_signal(
                        conn, origin=candidate.origin, destination=candidate.destination,
                        departure_date=candidate.departure_date, return_date=candidate.return_date,
                        current_price=candidate.price, now=run_started_at,
                        further_drop_threshold=config.dedup.further_drop_threshold,
                        reappear_gap_days=config.dedup.reappear_gap_days,
                    )
                except Exception:
                    logger.exception(
                        "Echec d'evaluation dedup (signal Google) pour %s -> %s (ignore, pas envoye)",
                        candidate.origin, candidate.destination,
                    )
                    continue

                if not decision.should_notify:
                    logger.info(
                        "Signal Google supprime par dedoublonnage (%s): %s -> %s %s %s",
                        decision.reason, candidate.origin, candidate.destination, candidate.price, candidate.currency,
                    )
                    continue

                signal_message = GoogleSignalMessage(
                    origin=candidate.origin, destination=candidate.destination,
                    destination_name=candidate.destination_name, price=candidate.price,
                    currency=candidate.currency, departure_date=candidate.departure_date,
                    return_date=candidate.return_date, airline=candidate.airline, stops=candidate.stops,
                    price_level=price_level,
                    source_url=(data.get("search_metadata") or {}).get("google_flights_url"),
                )
                try:
                    send_google_signal_notification(
                        config.secrets.telegram_bot_token, config.secrets.telegram_chat_id, signal_message
                    )
                except TelegramError:
                    logger.exception(
                        "Echec d'envoi du signal Google pour %s -> %s (ignore, retente demain car non enregistre)",
                        candidate.origin, candidate.destination,
                    )
                    continue

                insert_google_signal_notification(
                    conn,
                    GoogleSignalNotification(
                        notified_at=datetime.now(timezone.utc).isoformat(),
                        origin=candidate.origin, destination=candidate.destination,
                        departure_date=candidate.departure_date, return_date=candidate.return_date,
                        price=candidate.price, currency=candidate.currency, price_level=price_level,
                        observation_id=candidate.obs_id,
                    ),
                )
                sent += 1
    except Exception:
        logger.exception("Echec inattendu pendant la verification cold-start (ignore, run continue)")

    if checked > 0:
        # Comptees a part des explore calls (deja incrementees plus haut dans run_once) — meme
        # philosophie "1 requete = 1 tentative search(), succes ou echec" que SerpApiClient.search.
        increment_requests_used(conn, month_key, count=checked)

    return checked, sent


def _build_flight_observation(raw: RawObservation, observed_at: str) -> Optional[FlightObservation]:
    if not raw.is_exploitable:
        return None

    travel_period_bucket = compute_travel_period_bucket(raw.departure_date)
    if travel_period_bucket is None:
        return None  # garde-fou : is_exploitable implique departure_date present, mais un
        # format de date invalide reste theoriquement possible (voir bucketing.py)

    return FlightObservation(
        observed_at=observed_at,
        origin=raw.origin,
        destination=raw.destination,
        destination_name=raw.destination_name,
        departure_date=raw.departure_date,
        return_date=raw.return_date,
        price=raw.price,
        currency=raw.currency,
        airline=raw.airline,
        stops=raw.stops,
        duration_minutes=raw.duration_minutes,
        source=raw.source,
        source_url=raw.source_url,
        # Pas de fallback travel_duration ici : le Spike confirme que end_date est fiablement
        # present dans les reponses google_travel_explore (voir spike/SPIKE_NOTES.md) — si
        # jamais absent malgre tout, trip_length_nights devient simplement None et le filtre
        # de duree est desactive pour cette observation (voir repository.query_comparable_prices).
        trip_length_nights=compute_trip_length_nights(raw.departure_date, raw.return_date),
        travel_period_bucket=travel_period_bucket,
        stops_bucket=compute_stops_bucket(raw.stops),
    )


def _dedup_and_notify(
    conn,
    config: Config,
    triggered_deals: list[tuple[int, DealMessage]],
    run_started_at: datetime,
) -> int:
    to_send: list[tuple[int, DealMessage]] = []
    for obs_id, deal in triggered_deals:
        try:
            decision = should_notify(
                conn,
                origin=deal.origin,
                destination=deal.destination,
                departure_date=deal.departure_date,
                return_date=deal.return_date,
                current_price=deal.price,
                now=run_started_at,
                further_drop_threshold=config.dedup.further_drop_threshold,
                reappear_gap_days=config.dedup.reappear_gap_days,
            )
        except Exception:
            logger.exception(
                "Echec d'evaluation dedup pour %s -> %s (deal ignore, pas notifie)", deal.origin, deal.destination
            )
            continue

        if decision.should_notify:
            to_send.append((obs_id, deal))
        else:
            logger.info(
                "Deal supprime par dedoublonnage (%s): %s -> %s %s %s",
                decision.reason,
                deal.origin,
                deal.destination,
                deal.price,
                deal.currency,
            )

    if not config.notification.telegram or not to_send:
        return 0

    notified_count = 0
    for index, (obs_id, deal) in enumerate(to_send):
        try:
            send_deal_notification(config.secrets.telegram_bot_token, config.secrets.telegram_chat_id, deal)
        except TelegramError:
            logger.exception(
                "Echec d'envoi Telegram pour %s -> %s (ignore, run continue, retente demain "
                "car non enregistre comme notifie)",
                deal.origin,
                deal.destination,
            )
            continue

        # notified_deals n'est ecrit qu'APRES un envoi confirme reussi (spec section 12) :
        # un echec Telegram ne doit jamais etre marque "deja notifie", sinon un vrai deal
        # serait perdu silencieusement en cas de panne Telegram.
        insert_notification(
            conn,
            NotifiedDeal(
                notified_at=datetime.now(timezone.utc).isoformat(),
                origin=deal.origin,
                destination=deal.destination,
                departure_date=deal.departure_date,
                return_date=deal.return_date,
                price=deal.price,
                currency=deal.currency,
                score=deal.score,
                discount=deal.discount,
                observation_id=obs_id,
            ),
        )
        notified_count += 1

        if index < len(to_send) - 1:
            time.sleep(config.notification.send_delay_seconds)

    return notified_count
