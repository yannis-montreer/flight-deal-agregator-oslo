"""Planification multi-alertes (runner.py) et registre des cles (keys.py). Aucun appel reseau."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trip_watch import keys
from trip_watch.runner import WatchScheduler, load_watches, run_cycle

REPO_ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("SEARCH_SERPAPI_KEY", "SERPAPI_KEY", "SERPAPI_KEY_3", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(name, "x")


@pytest.fixture
def watch_dir(tmp_path):
    d = tmp_path / "watches"
    d.mkdir()
    shutil.copy(REPO_ROOT / "watches" / "santiago.yaml", d / "santiago.yaml")
    return d


def _add_watch(watch_dir: Path, name: str, **replace) -> Path:
    text = (watch_dir / "santiago.yaml").read_text(encoding="utf-8").replace("db_file: trip_watch.db", f"db_file: trip_watch_{name}.db")
    for old, new in replace.items():
        text = text.replace(old, new)
    path = watch_dir / f"{name}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class Recorder:
    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    def __call__(self, config, db_path):
        self.calls.append((config.watch_id, Path(db_path).name))
        if config.watch_id in self.fail_for:
            raise RuntimeError("boom")


def _cycle(scheduler, watch_dir, recorder, now, data_dir=Path("/data")):
    executed, _ = run_cycle(scheduler, watch_dir, data_dir, recorder, now, lambda: now)
    return executed


# 12/11/2026 : Oslo = UTC+1 (CET) -> 10:30 Oslo = 09:30 UTC
BEFORE = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
AT = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
AFTER = datetime(2026, 11, 12, 9, 35, tzinfo=UTC)


def test_santiago_keeps_its_historical_database(watch_dir):
    configs, errors = load_watches(watch_dir)
    assert errors == {}
    assert configs["santiago"].db_file == "trip_watch.db"
    assert configs["santiago"].enabled is True


def test_runs_once_at_schedule_and_never_twice_a_day(watch_dir):
    scheduler, rec = WatchScheduler(), Recorder()
    assert _cycle(scheduler, watch_dir, rec, BEFORE) == []
    assert _cycle(scheduler, watch_dir, rec, AT) == ["santiago"]
    assert rec.calls == [("santiago", "trip_watch.db")]
    assert _cycle(scheduler, watch_dir, rec, AFTER) == []
    # Heure decalee plus tard le meme jour : pas de second run aujourd'hui.
    (watch_dir / "santiago.yaml").write_text(
        (watch_dir / "santiago.yaml").read_text(encoding="utf-8").replace('schedule: "10:30"', 'schedule: "15:00"'),
        encoding="utf-8",
    )
    assert _cycle(scheduler, watch_dir, rec, datetime(2026, 11, 12, 14, 0, tzinfo=UTC)) == []
    assert _cycle(scheduler, watch_dir, rec, datetime(2026, 11, 13, 14, 0, tzinfo=UTC)) == ["santiago"]


def test_start_after_schedule_waits_until_tomorrow(watch_dir):
    """Redemarrage a 10:35 : le run de 10:30 n'est PAS rejoue (pas de doublon Telegram)."""
    scheduler, rec = WatchScheduler(), Recorder()
    assert _cycle(scheduler, watch_dir, rec, AFTER) == []
    assert _cycle(scheduler, watch_dir, rec, datetime(2026, 11, 13, 9, 30, tzinfo=UTC)) == ["santiago"]


def test_new_watch_is_picked_up_without_restart_and_uses_its_own_db(watch_dir):
    scheduler, rec = WatchScheduler(), Recorder()
    _cycle(scheduler, watch_dir, rec, BEFORE)
    _add_watch(watch_dir, "lima", **{"destination: SCL": "destination: LIM"})
    _cycle(scheduler, watch_dir, rec, datetime(2026, 11, 12, 9, 5, tzinfo=UTC))  # relecture periodique
    assert _cycle(scheduler, watch_dir, rec, AT) == ["lima", "santiago"]
    assert ("lima", "trip_watch_lima.db") in rec.calls


def test_paused_and_invalid_watches_do_not_run(watch_dir):
    scheduler, rec = WatchScheduler(), Recorder()
    _add_watch(watch_dir, "pause", **{"enabled: true": "enabled: false"})
    (watch_dir / "casse.yaml").write_text("origin: [", encoding="utf-8")
    _cycle(scheduler, watch_dir, rec, BEFORE)
    executed, errors = run_cycle(scheduler, watch_dir, Path("/data"), rec, AT, lambda: AT)
    assert executed == ["santiago"]
    assert "casse" in errors


def test_failing_watch_does_not_block_others(watch_dir):
    scheduler, rec = WatchScheduler(), Recorder(fail_for={"lima"})
    _add_watch(watch_dir, "lima")
    _cycle(scheduler, watch_dir, rec, BEFORE)
    assert _cycle(scheduler, watch_dir, rec, AT) == ["lima", "santiago"]
    assert _cycle(scheduler, watch_dir, rec, AFTER) == []  # pas de nouvel essai le meme jour


def test_removed_watch_stops(watch_dir):
    scheduler, rec = WatchScheduler(), Recorder()
    _add_watch(watch_dir, "lima")
    _cycle(scheduler, watch_dir, rec, BEFORE)
    (watch_dir / "lima.yaml").unlink()
    assert _cycle(scheduler, watch_dir, rec, AT) == ["santiago"]


def test_hidden_and_history_files_are_ignored(watch_dir):
    (watch_dir / ".santiago.yaml.tmp").write_text("x", encoding="utf-8")
    (watch_dir / ".history").mkdir()
    shutil.copy(watch_dir / "santiago.yaml", watch_dir / ".history" / "santiago-20261112.yaml")
    configs, errors = load_watches(watch_dir)
    assert list(configs) == ["santiago"] and errors == {}


def test_sleep_is_capped_to_rescan_interval(watch_dir):
    scheduler = WatchScheduler()
    _cycle(scheduler, watch_dir, Recorder(), BEFORE)
    assert scheduler.seconds_to_sleep(BEFORE) == 300
    assert scheduler.seconds_to_sleep(datetime(2026, 11, 12, 9, 29, tzinfo=UTC)) == 60


# ---------- registre des cles ----------

def test_registry_lists_names_and_counters_never_secrets(tmp_path):
    environ = {"SERPAPI_KEY": "secret-main", "SEARCH_SERPAPI_KEY": "secret-search", "SERPAPI_KEY_3": " ",
               "TELEGRAM_BOT_TOKEN": "tok", "PATH": "/bin"}

    def fetch(api_key):
        if api_key == "secret-search":
            raise RuntimeError(f"https://serpapi.com/account.json?api_key={api_key} 401")
        return {"searches_per_month": 250, "total_searches_left": 244, "api_key": api_key,
                "account_email": "someone@example.com"}

    registry = keys.build_registry(environ, fetch)
    assert [k["name"] for k in registry["keys"]] == ["SEARCH_SERPAPI_KEY", "SERPAPI_KEY"]  # vide ignoree
    main = registry["keys"][1]
    assert main["searches_per_month"] == 250 and main["searches_left"] == 244
    assert registry["keys"][0]["error"] == "RuntimeError"
    keys.write_registry(tmp_path, registry)
    text = (tmp_path / "keys.json").read_text(encoding="utf-8")
    assert "secret" not in text and "example.com" not in text and "tok" not in text
    assert json.loads(text)["keys"][1]["name"] == "SERPAPI_KEY"


def test_historical_min_compares_same_currency_only(tmp_path):
    from trip_watch.db import get_connection, get_historical_min, insert_observation
    conn = get_connection(tmp_path / "t.db")
    common = dict(observed_at="2026-11-12T09:30:00+00:00", destination="SCL", departure_date="2027-01-15",
                  return_date="2027-02-19", airline=None, stops=1, price_level=None, error=None)
    insert_observation(conn, price=9000.0, currency="NOK", **common)
    insert_observation(conn, price=800.0, currency="EUR", **common)
    assert get_historical_min(conn, "SCL", "2027-01-15", "2027-02-19", currency="NOK") == 9000.0
    assert get_historical_min(conn, "SCL", "2027-01-15", "2027-02-19", currency="EUR") == 800.0
    conn.close()
