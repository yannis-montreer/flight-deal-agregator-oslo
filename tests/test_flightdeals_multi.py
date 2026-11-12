"""Veilles "bons plans" multiples (flightdeals/multi.py) : chargement, bases, planification,
titres des messages. Aucun appel reseau."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flightdeals import multi
from flightdeals.config import ConfigError, load_config
from flightdeals.notify.telegram import with_title
from flightdeals.scheduler import WatchScheduler

REPO_ROOT = Path(__file__).resolve().parent.parent
UTC = timezone.utc
BEFORE = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)   # 10:00 Oslo
AT = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)      # 10:30 Oslo
AFTER = datetime(2026, 11, 12, 9, 40, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("SERPAPI_KEY", "SERPAPI_KEY_3", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(name, "x")


@pytest.fixture
def layout(tmp_path):
    main = tmp_path / "config" / "config.yaml"
    main.parent.mkdir()
    shutil.copy(REPO_ROOT / "config" / "config.yaml", main)
    deals = tmp_path / "deals"
    deals.mkdir()
    return main, deals, tmp_path / "data"


def _add_deal(deals: Path, main: Path, name: str, extra: str = "", **replace) -> Path:
    text = main.read_text(encoding="utf-8")
    for old, new in replace.items():
        text = text.replace(old, new)
    path = deals / f"{name}.yaml"
    path.write_text(f'name: "Veille {name}"\napi_key_env: SERPAPI_KEY_3\ndb_file: flightdeals_{name}.db\n{extra}' + text,
                    encoding="utf-8")
    return path


class Recorder:
    def __init__(self, fail_for=()):
        self.calls, self.fail_for = [], set(fail_for)

    def __call__(self, config, db_path):
        self.calls.append((config.veille_id, Path(db_path).name, config.name))
        if config.veille_id in self.fail_for:
            raise RuntimeError("boom")


def _cycle(scheduler, layout, rec, now):
    main, deals, data = layout
    executed, _ = multi.run_cycle(scheduler, main, deals, data, data / "flightdeals.db", rec, now, lambda: now)
    return executed


def test_main_alone_keeps_historical_db_and_messages(layout):
    main, deals, data = layout
    configs, errors = multi.load_veilles(main, deals)
    assert list(configs) == ["main"] and errors == {}
    config = configs["main"]
    assert config.name is None and config.api_key_env == "SERPAPI_KEY"
    assert multi.db_path_for(config, data, data / "flightdeals.db").name == "flightdeals.db"


def test_no_deals_dir_means_main_only(layout):
    main, _, _ = layout
    configs, _ = multi.load_veilles(main, None)
    assert list(configs) == ["main"]


def test_second_veille_has_own_key_db_and_name(layout):
    main, deals, data = layout
    _add_deal(deals, main, "italie")
    configs, errors = multi.load_veilles(main, deals)
    assert errors == {} and set(configs) == {"main", "italie"}
    italie = configs["italie"]
    assert italie.name == "Veille italie" and italie.api_key_env == "SERPAPI_KEY_3"
    assert italie.secrets.serpapi_key == "x"
    assert multi.db_path_for(italie, data, data / "flightdeals.db").name == "flightdeals_italie.db"


def test_runs_each_veille_once_at_its_time(layout):
    main, deals, _ = layout
    _add_deal(deals, main, "italie")
    scheduler, rec = WatchScheduler(), Recorder()
    assert _cycle(scheduler, layout, rec, BEFORE) == []
    assert _cycle(scheduler, layout, rec, AT) == ["italie", "main"]
    assert ("main", "flightdeals.db", None) in rec.calls
    assert ("italie", "flightdeals_italie.db", "Veille italie") in rec.calls
    assert _cycle(scheduler, layout, rec, AFTER) == []


def test_restart_after_schedule_does_not_rerun_today(layout):
    scheduler, rec = WatchScheduler(), Recorder()
    assert _cycle(scheduler, layout, rec, AFTER) == []
    assert _cycle(scheduler, layout, rec, datetime(2026, 11, 13, 9, 30, tzinfo=UTC)) == ["main"]


def test_paused_invalid_and_failing_veilles(layout):
    main, deals, _ = layout
    _add_deal(deals, main, "pause", **{"enabled: true": "enabled: false"})
    _add_deal(deals, main, "casse", **{"min_budget_reserve: 5": "min_budget_reserve: [", })
    _add_deal(deals, main, "echec")
    scheduler, rec = WatchScheduler(), Recorder(fail_for={"echec"})
    _cycle(scheduler, layout, rec, BEFORE)
    main_path, deals_dir, data = layout
    executed, errors = multi.run_cycle(scheduler, main_path, deals_dir, data, data / "flightdeals.db", rec, AT, lambda: AT)
    assert executed == ["echec", "main"] and "casse" in errors


def test_main_paused_with_collection_enabled_false(layout):
    main, deals, _ = layout
    main.write_text(main.read_text(encoding="utf-8").replace("enabled: true", "enabled: false", 1), encoding="utf-8")
    scheduler, rec = WatchScheduler(), Recorder()
    _cycle(scheduler, layout, rec, BEFORE)
    assert _cycle(scheduler, layout, rec, AT) == []


def test_api_key_env_is_required_when_set(layout, monkeypatch):
    main, deals, _ = layout
    path = _add_deal(deals, main, "italie")
    monkeypatch.delenv("SERPAPI_KEY_3")
    with pytest.raises(ConfigError, match="SERPAPI_KEY_3"):
        load_config(path)
    assert load_config(path, require_secrets=False).api_key_env == "SERPAPI_KEY_3"


@pytest.mark.parametrize("line,message", [
    ('name: ""\n', "name"), ("api_key_env: 'cle secrete'\n", "api_key_env"), ("db_file: ../x.db\n", "db_file"),
])
def test_invalid_new_fields(layout, line, message):
    main, _, _ = layout
    bad = main.parent / "bad.yaml"
    bad.write_text(line + main.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(bad, require_secrets=False)


def test_titles_only_for_named_veilles():
    assert with_title("", "texte") == "texte"
    assert with_title("Italie", "texte") == "\U0001F3F7 Italie\ntexte"
