"""Projection de la consommation d'une cle SerpApi jusqu'a sa prochaine reinitialisation.

projete = deja consomme (compteur SerpApi) + somme, pour chaque alerte qui utilise la cle,
de (recherches par run) x (runs restants avant la date de renouvellement).

Un run compte s'il a lieu APRES maintenant et a une date locale STRICTEMENT anterieure a la
date de renouvellement (les runs du jour de renouvellement tombent dans le cycle suivant).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

WARN_RATIO = 0.95


@dataclass(frozen=True)
class Consumer:
    label: str
    per_run: int
    schedule: str   # "HH:MM"
    timezone: str   # nom IANA


@dataclass
class Projection:
    used: int
    planned: int
    quota: int
    renewal: date
    days_left: int
    exhaust_on: Optional[date] = None        # 1er jour ou le quota serait depasse
    planned_by: dict = field(default_factory=dict)

    @property
    def projected(self) -> int:
        return self.used + self.planned

    @property
    def ratio(self) -> float:
        return self.projected / self.quota if self.quota else 1.0

    @property
    def warn(self) -> bool:
        return self.ratio >= WARN_RATIO

    def percent(self, value: int) -> int:
        return min(100, round(100 * value / self.quota)) if self.quota else 100


def run_times(now: datetime, schedule: str, tz_name: str, renewal: date) -> list[datetime]:
    tz = ZoneInfo(tz_name)
    hour, minute = map(int, schedule.split(":"))
    local_now = now.astimezone(tz)
    times, day = [], local_now.date()
    while day < renewal:
        run = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
        if run > local_now:
            times.append(run)
        day += timedelta(days=1)
    return times


def project(used: int, quota: int, renewal: date, consumers: list[Consumer], now: datetime,
            local_tz: str = "Europe/Oslo") -> Projection:
    events = sorted(
        (when, c.per_run, c.label)
        for c in consumers if c.per_run > 0
        for when in run_times(now, c.schedule, c.timezone, renewal)
    )
    planned_by: dict[str, int] = {}
    cumulative, exhaust_on = used, None
    for when, per_run, label in events:
        planned_by[label] = planned_by.get(label, 0) + per_run
        cumulative += per_run
        if exhaust_on is None and cumulative > quota:
            exhaust_on = when.date()
    if exhaust_on is None and used > quota:
        exhaust_on = now.astimezone(ZoneInfo(local_tz)).date()
    days_left = max(0, (renewal - now.astimezone(ZoneInfo(local_tz)).date()).days)
    return Projection(used, cumulative - used, quota, renewal, days_left, exhaust_on, planned_by)
