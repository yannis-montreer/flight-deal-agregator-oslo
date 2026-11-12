"""Projection de consommation par cle jusqu'a sa reinitialisation (webui/projection.py)."""
from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from webui.app import create_app
from webui.projection import Consumer, project, run_times

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent
SANTIAGO = Consumer("Santiago", 7, "10:30", "Europe/Oslo")
FLIGHTDEALS = Consumer("Bons plans", 6, "10:30", "Europe/Oslo")


def test_runs_counted_until_day_before_renewal():
    # 12/11 15:00 Oslo -> runs des 13, 14, 15, 16, 17 ; le 18 (renouvellement) n'en fait pas partie.
    now = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
    times = run_times(now, "10:30", "Europe/Oslo", date(2026, 11, 18))
    assert [t.day for t in times] == [13, 14, 15, 16, 17]


def test_todays_run_counts_if_still_ahead():
    now = datetime(2026, 11, 12, 8, 0, tzinfo=UTC)  # 09:00 Oslo
    assert run_times(now, "10:30", "Europe/Oslo", date(2026, 11, 13))[0].day == 12


def test_projection_sums_alerts_on_the_key():
    now = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
    p = project(7, 250, date(2026, 11, 19), [SANTIAGO], now)
    assert p.planned == 7 * 6 and p.projected == 49 and p.days_left == 7
    assert not p.warn and p.exhaust_on is None
    assert p.planned_by == {"Santiago": 42}


def test_warns_at_95_percent_and_dates_exhaustion():
    now = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
    p = project(200, 250, date(2026, 11, 19), [SANTIAGO, FLIGHTDEALS], now)
    # 13 recherches/jour : 213, 226, 239, 252 -> depasse le 16/11
    assert p.warn and p.exhaust_on == date(2026, 11, 16)
    # 4 runs (13 au 16) x 10 : 200 + 40 = 240 = 96 % -> avertissement sans epuisement
    near = project(200, 250, date(2026, 11, 17), [Consumer("A", 10, "10:30", "Europe/Oslo")], now)
    assert near.projected == 240 and near.warn and near.exhaust_on is None
    # 195 + 40 = 235 = 94 % -> pas d'avertissement
    assert not project(195, 250, date(2026, 11, 17), [Consumer("A", 10, "10:30", "Europe/Oslo")], now).warn


def test_paused_alerts_and_past_renewal():
    now = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
    assert project(10, 250, date(2026, 11, 19), [Consumer("Pause", 0, "10:30", "Europe/Oslo")], now).planned == 0
    stale = project(10, 250, date(2026, 11, 10), [SANTIAGO], now)  # registre pas encore rafraichi
    assert stale.planned == 0 and stale.days_left == 0


def test_index_shows_projection_and_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBUI_KEY_QUOTAS", raising=False)
    for d in ("config", "watches", "status"):
        (tmp_path / d).mkdir()
    shutil.copy(REPO_ROOT / "config" / "config.yaml", tmp_path / "config" / "config.yaml")
    shutil.copy(REPO_ROOT / "watches" / "santiago.yaml", tmp_path / "watches" / "santiago.yaml")
    renewal = (date.today() + timedelta(days=5)).isoformat()
    (tmp_path / "status" / "keys.json").write_text(json.dumps({"updated_at": "2026-11-12T12:00:00+00:00", "keys": [
        {"name": "SEARCH_SERPAPI_KEY", "searches_per_month": 250, "searches_left": 10,
         "used_this_period": 240, "renewal_date": renewal, "error": None},
        {"name": "SERPAPI_KEY", "searches_per_month": 250, "searches_left": 244,
         "used_this_period": 6, "renewal_date": renewal, "error": None},
    ]}), encoding="utf-8")
    client = TestClient(create_app(tmp_path / "config" / "config.yaml", tmp_path / "watches",
                                   status_dir=tmp_path / "status"), base_url="http://127.0.0.1")
    page = client.get("/").text
    assert f"réinitialisation du <strong>{date.fromisoformat(renewal).strftime('%d/%m')}</strong>" in page
    assert "Déjà consommé : 240" in page and "Quota épuisé vers le" in page
    assert "Déjà consommé : 6" in page
