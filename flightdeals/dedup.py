"""Regle de dedoublonnage des notifications (spec section 12).

Cle logique = (origin, destination, departure_date, return_date) : un changement de dates
est donc deja une cle differente, sans code special — should_notify n'a que 2 cas reels a
gerer (reapparition, baisse suffisante), pas 3 (voir plan).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from flightdeals.db.repository import get_last_google_signal_notification, get_last_notification


@dataclass(frozen=True)
class DedupDecision:
    should_notify: bool
    reason: str  # log/debug : "first_notification" | "reappeared" | "further_drop" | "suppressed_duplicate"


def _evaluate_dedup(
    last: Optional[sqlite3.Row],
    *,
    current_price: float,
    now: datetime,
    further_drop_threshold: float,
    reappear_gap_days: int,
) -> DedupDecision:
    """Logique pure (aucun acces DB) partagee par should_notify (deals statistiques) et
    should_notify_google_signal (signal Google en periode de cold-start) — meme regle,
    appliquee a 2 tables de notifications totalement separees (voir dedup_google.py note
    dans repository.py : jamais de suppression croisee entre les deux types).

    Ordre de verification : reapparition (gap) avant baisse supplementaire. Si les deux
    conditions sont vraies simultanement, la raison rapportee est "reappeared" — le gap est
    la condition la plus forte (le deal a disparu puis revient), la baisse de prix devient
    secondaire dans ce cas.
    """
    if last is None:
        return DedupDecision(should_notify=True, reason="first_notification")

    last_notified_at = datetime.fromisoformat(last["notified_at"])
    if last_notified_at.tzinfo is None:
        # Filet de securite si une ligne stockee manque d'offset (ne devrait pas arriver :
        # insert_notification stocke toujours du UTC-aware via .isoformat()) — pas une
        # supposition sur un appelant, juste une tolerance sur une donnee deja en base.
        last_notified_at = last_notified_at.replace(tzinfo=timezone.utc)

    gap_days = (now - last_notified_at).days
    if gap_days >= reappear_gap_days:
        return DedupDecision(should_notify=True, reason="reappeared")

    drop_threshold_price = last["price"] * (1 - further_drop_threshold)
    if current_price <= drop_threshold_price:
        return DedupDecision(should_notify=True, reason="further_drop")

    return DedupDecision(should_notify=False, reason="suppressed_duplicate")


def should_notify(
    conn: sqlite3.Connection,
    *,
    origin: str,
    destination: str,
    departure_date: str,
    return_date: Optional[str],
    current_price: float,
    now: datetime,
    further_drop_threshold: float,
    reappear_gap_days: int,
) -> DedupDecision:
    """`now` DOIT etre timezone-aware UTC (datetime.now(timezone.utc)) — volontairement pas
    de coercion silencieuse d'un naive ici : un appelant qui passe une heure locale naive est
    un bug a corriger, pas a masquer (voir plan, philosophie "fail-fast")."""
    last = get_last_notification(conn, origin, destination, departure_date, return_date)
    return _evaluate_dedup(
        last, current_price=current_price, now=now,
        further_drop_threshold=further_drop_threshold, reappear_gap_days=reappear_gap_days,
    )


def should_notify_google_signal(
    conn: sqlite3.Connection,
    *,
    origin: str,
    destination: str,
    departure_date: str,
    return_date: Optional[str],
    current_price: float,
    now: datetime,
    further_drop_threshold: float,
    reappear_gap_days: int,
) -> DedupDecision:
    """Meme regle que should_notify, mais lue/ecrite dans google_signal_notifications — une
    table separee de notified_deals expres : un signal Google (non confirme par notre
    historique) ne doit jamais pouvoir supprimer, ni etre supprime par, un vrai deal
    statistique sur la meme cle (origin, destination, dates). Voir pipeline._check_cold_start_signals."""
    last = get_last_google_signal_notification(conn, origin, destination, departure_date, return_date)
    return _evaluate_dedup(
        last, current_price=current_price, now=now,
        further_drop_threshold=further_drop_threshold, reappear_gap_days=reappear_gap_days,
    )
