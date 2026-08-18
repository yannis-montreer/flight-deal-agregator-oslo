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
from flightdeals.analysis.scoring import evaluate_deal
from flightdeals.analysis.statistics import compute_price_stats
from flightdeals.collectors.flight_search import RawObservation, fetch_all_explore_destinations
from flightdeals.collectors.serpapi_client import SerpApiClient
from flightdeals.config import Config
from flightdeals.db.connection import get_connection
from flightdeals.db.repository import (
    FlightObservation,
    NotifiedDeal,
    get_requests_used,
    increment_requests_used,
    insert_notification,
    insert_observation,
    query_comparable_prices,
)
from flightdeals.dedup import should_notify
from flightdeals.notify.telegram import DealMessage, TelegramError, send_deal_notification

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunSummary:
    """Retourne par run_once() — sert au log de fin de run et aux tests d'integration."""

    skipped_budget: bool
    observations_collected: int
    observations_stored: int
    deals_triggered: int
    deals_notified: int


def run_once(config: Config, db_path: "Path | str") -> RunSummary:
    run_started_at = datetime.now(timezone.utc)
    run_started_at_iso = run_started_at.isoformat()
    window_start_iso = (run_started_at - timedelta(days=config.deal.history_window_days)).isoformat()
    month_key = run_started_at.strftime("%Y-%m")

    conn = get_connection(db_path)
    try:
        with SerpApiClient(api_key=config.secrets.serpapi_key) as client:
            if _budget_exhausted(client, conn, config, month_key):
                return RunSummary(
                    skipped_budget=True,
                    observations_collected=0,
                    observations_stored=0,
                    deals_triggered=0,
                    deals_notified=0,
                )

            raw_observations = fetch_all_explore_destinations(
                client,
                origin=config.origin,
                currency=config.currency,
                travel_durations=config.serpapi.travel_durations,
                arrival_area_id=config.serpapi.arrival_area_id,
            )
            # Approximation deliberee : compte 1 requete par duree TENTEE (pas par retry HTTP
            # interne). Simple, auditable, et surestime plutot que sous-estime les jours ou le
            # quota coupe la collecte en cours de route (la marge budget/200 est large, voir plan).
            increment_requests_used(conn, month_key, count=len(config.serpapi.travel_durations))

        stored_count = 0
        triggered_deals: list[tuple[int, DealMessage]] = []

        for raw in raw_observations:
            try:
                outcome = _evaluate_one(conn, config, raw, run_started_at_iso, window_start_iso)
            except Exception:
                logger.exception("Echec de traitement d'une observation (ignoree, run continue): %r", raw)
                continue

            if outcome is None:
                continue  # non exploitable (pas de prix/destination/date) -> pas stockee

            stored_count += 1
            obs_id, deal_message = outcome
            if deal_message is not None:
                triggered_deals.append((obs_id, deal_message))

        notified_count = _dedup_and_notify(conn, config, triggered_deals, run_started_at)

        logger.info(
            "Run termine: %d observations collectees, %d stockees, %d deals declenches, %d notifies",
            len(raw_observations),
            stored_count,
            len(triggered_deals),
            notified_count,
        )
        return RunSummary(
            skipped_budget=False,
            observations_collected=len(raw_observations),
            observations_stored=stored_count,
            deals_triggered=len(triggered_deals),
            deals_notified=notified_count,
        )
    finally:
        conn.close()


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
) -> Optional[tuple[int, Optional[DealMessage]]]:
    """None si l'observation n'est pas stockable (pas de prix/destination/date exploitable).
    Sinon (id_observation_stockee, DealMessage_ou_None) — le DealMessage n'est present QUE si
    la regle de declenchement du spec (section 10) est satisfaite ; obs_id est TOUJOURS
    retourne des qu'une observation est stockee, meme sans deal declenche."""
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
            minimum_discount=config.deal.minimum_discount,
            minimum_observations=config.deal.minimum_observations,
            percentile_threshold=config.deal.percentile_threshold,
            discount_cap=config.scoring.discount_cap,
            confidence_saturation_count=config.scoring.confidence_saturation_count,
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
        return obs_id, None

    if not evaluation.triggers:
        return obs_id, None

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
        discount=evaluation.discount,
        score=evaluation.score,
        source_url=obs.source_url,
    )
    return obs_id, deal_message


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
