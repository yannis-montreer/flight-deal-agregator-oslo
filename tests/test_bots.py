"""Bots Telegram multiples et cles SerpApi ajoutees depuis l'interface. Aucun appel reseau."""
from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flightdeals import secret_store
from flightdeals.notify import telegram
from flightdeals.notify.telegram import TelegramError
from trip_watch import keys
from trip_watch import tracker
from trip_watch.config import ConfigError, load_config
from webui import telegram_tools
from webui.app import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "123456789:AAHfakefakefakefakefakefakefakefake1234"
SERP = "a" * 64


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    d = tmp_path / "secrets"
    secret_store.write_store(d, {"serpapi_keys": {"SERPAPI_KEY_ANNA": SERP},
                                 "bots": {"anna": {"alias": "Anna", "token": TOKEN, "chat_id": "42"}}})
    monkeypatch.setenv("FLIGHTDEALS_SECRETS_DIR", str(d))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "main-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.delenv("SERPAPI_KEY_ANNA", raising=False)
    return d


def test_resolve_key_env_first_then_store(secrets, monkeypatch):
    assert secret_store.resolve_key("SERPAPI_KEY_ANNA") == SERP
    monkeypatch.setenv("SERPAPI_KEY_ANNA", "fromenv")
    assert secret_store.resolve_key("SERPAPI_KEY_ANNA") == "fromenv"
    assert secret_store.resolve_key("SERPAPI_KEY_NOPE") == ""


def test_notify_parsing_and_resolution(secrets):
    assert secret_store.parse_notify(None) == ("main",)
    assert secret_store.parse_notify(["anna", "main", "anna"]) == ("anna", "main")
    with pytest.raises(ValueError):
        secret_store.parse_notify([])
    bots = secret_store.resolve_bots(["main", "anna"])
    assert [(b.bot_id, b.chat_id) for b in bots] == [("main", "1"), ("anna", "42")]
    assert TOKEN not in repr(bots)
    with pytest.raises(secret_store.StoreError, match="bob"):
        secret_store.resolve_bots(["bob"])
    assert secret_store.mask(TOKEN) == "••••1234"


def _watch_file(tmp_path, extra=""):
    text = (REPO_ROOT / "watches" / "santiago.yaml").read_text(encoding="utf-8")
    text = text.replace("api_key_env: SEARCH_SERPAPI_KEY", "api_key_env: SERPAPI_KEY_ANNA") + extra
    path = tmp_path / "lima.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_trip_watch_config_uses_store_key_and_bots(secrets, tmp_path):
    config = load_config(_watch_file(tmp_path, "\nnotify: [anna, main]\n"))
    assert config.api_key == SERP and config.notify == ("anna", "main")
    assert [b.alias for b in config.bots] == ["Anna", "Principal (moi)"]
    with pytest.raises(ConfigError, match="inconnu"):
        load_config(_watch_file(tmp_path, "\nnotify: [bob]\n"))
    assert load_config(_watch_file(tmp_path, "\nnotify: [bob]\n"), require_secrets=False).notify == ("bob",)


def test_digest_goes_to_each_bot_quota_only_for_main(secrets, tmp_path, monkeypatch):
    config = load_config(_watch_file(tmp_path, "\nnotify: [anna, main]\n"))
    sent = []
    monkeypatch.setattr(tracker, "send_message", lambda token, chat, text: sent.append((chat, text)))
    result = tracker.DailyResult("2027-01-15", "2027-02-19", 9000.0, "NOK", "LATAM", 1, "low", None, None, None)
    tracker._send_daily_digest(config, result, True, 35, 180)
    assert [c for c, _ in sent] == ["42", "1"]
    assert "Quota SerpApi" not in sent[0][1] and "Quota SerpApi" in sent[1][1]


def test_send_to_bots_partial_failure(secrets, monkeypatch):
    bots = secret_store.resolve_bots(["anna", "main"])

    def fake_send(token, chat, text, timeout_seconds=15.0):
        if chat == "42":
            raise TelegramError("down")
    monkeypatch.setattr(telegram, "send_message", fake_send)
    assert telegram.send_to_bots(bots, "x") == ["main"]
    monkeypatch.setattr(telegram, "send_message", lambda *a, **k: (_ for _ in ()).throw(TelegramError("down")))
    with pytest.raises(TelegramError):
        telegram.send_to_bots(bots, "x")


def test_flightdeals_veille_sends_deals_to_its_bots(secrets, tmp_path, monkeypatch):
    from flightdeals.config import load_config as load_fd
    from flightdeals.pipeline import _title_kwargs
    monkeypatch.setenv("SERPAPI_KEY", "k")
    main = load_fd(REPO_ROOT / "config" / "config.yaml")
    assert "bots" not in _title_kwargs(main)  # veille historique : appel inchange
    text = (REPO_ROOT / "config" / "config.yaml").read_text(encoding="utf-8")
    path = tmp_path / "it.yaml"
    path.write_text("name: \"Italie\"\napi_key_env: SERPAPI_KEY_ANNA\nnotify: [anna]\n" + text, encoding="utf-8")
    cfg = load_fd(path)
    assert cfg.secrets.serpapi_key == SERP
    assert [b.bot_id for b in _title_kwargs(cfg)["bots"]] == ["anna"]
    assert cfg.secrets.telegram_chat_id == "1"  # recap/budget : toujours le bot principal


def test_registry_lists_store_keys_without_values(secrets):
    registry = keys.build_registry({"SERPAPI_KEY": "envkey"}, lambda k: {"searches_per_month": 250},
                                   keys.stored_keys(secrets))
    assert [(k["name"], k["source"]) for k in registry["keys"]] == [("SERPAPI_KEY", "env"), ("SERPAPI_KEY_ANNA", "interface")]
    assert SERP not in json.dumps(registry)


# ---------- interface ----------

class FakeTelegram:
    TelegramToolError = telegram_tools.TelegramToolError

    def __init__(self):
        self.tests = []

    def find_chats(self, token):
        return [telegram_tools.Chat("777", "Bob (@bob)")]

    def bot_username(self, token):
        return "bob_flights_bot"

    def send_test(self, token, chat_id, alias):
        self.tests.append((chat_id, alias))


@pytest.fixture
def ui(tmp_path, secrets, monkeypatch):
    monkeypatch.delenv("WEBUI_KEY_QUOTAS", raising=False)
    for d in ("config", "watches", "status"):
        (tmp_path / d).mkdir()
    shutil.copy(REPO_ROOT / "config" / "config.yaml", tmp_path / "config" / "config.yaml")
    shutil.copy(REPO_ROOT / "watches" / "santiago.yaml", tmp_path / "watches" / "santiago.yaml")
    (tmp_path / "status" / "keys.json").write_text(json.dumps({"updated_at": "x", "keys": [
        {"name": n, "searches_per_month": 250} for n in ("SEARCH_SERPAPI_KEY", "SERPAPI_KEY")]}), encoding="utf-8")
    fake = FakeTelegram()
    client = TestClient(create_app(tmp_path / "config" / "config.yaml", tmp_path / "watches",
                                   status_dir=tmp_path / "status", secrets_dir=secrets,
                                   fetch_account=lambda k: {"searches_per_month": 250}, telegram=fake),
                        base_url="http://127.0.0.1")
    return client, tmp_path, fake


def _watch_form(**overrides):
    form = {"destination_name": "Malaga Bob", "origin": "OSL", "destination": "AGP", "departure_center": "2027-03-10",
            "departure_tolerance_days": "1", "duration_center_days": "7", "duration_tolerance_days": "1",
            "required_stops": "max1", "travel_class": "1", "currency": "NOK", "schedule": "10:30",
            "api_key_env": "SERPAPI_KEY_ANNA", "enabled": "on", "notify_shown": "1", "notify": ["anna", "main"]}
    form.update(overrides)
    return form


def test_add_bot_detect_then_save_token_never_shown(ui):
    c, tmp, fake = ui
    page = c.post("/bots/new", data={"alias": "Bob", "token": TOKEN, "chat_id": "", "action": "detect"})
    assert page.status_code == 200 and "Bob (@bob)" in page.text and 'value="777"' in page.text
    r = c.post("/bots/new", data={"alias": "Bob", "token": TOKEN, "chat_id": "777", "action": "save"}, follow_redirects=False)
    assert r.status_code == 303
    stored = secret_store.read_store(tmp / "secrets")["bots"]["bob"]
    assert (stored["chat_id"], stored["username"]) == ("777", "bob_flights_bot")
    listing = c.get("/bots").text
    assert "@bob_flights_bot" in listing and TOKEN not in listing and "••••1234" in listing
    assert TOKEN not in c.get("/bots/bob").text  # edition : token jamais renvoye
    # edition sans token : le token est conserve
    c.post("/bots/bob", data={"alias": "Bobby", "token": "", "chat_id": "778", "action": "save"})
    stored = secret_store.read_store(tmp / "secrets")["bots"]["bob"]
    assert (stored["alias"], stored["token"], stored["chat_id"]) == ("Bobby", TOKEN, "778")
    assert c.post("/bots/bob/test", follow_redirects=False).status_code == 303 and fake.tests == [("778", "Bobby")]


def test_bad_token_refused(ui):
    c, tmp, _ = ui
    r = c.post("/bots/new", data={"alias": "X", "token": "nope", "chat_id": "1", "action": "save"})
    assert r.status_code == 400 and "format inattendu" in r.text


def test_alert_with_two_bots_and_store_key(ui):
    c, tmp, _ = ui
    assert c.post("/watches/new", data=_watch_form(), follow_redirects=False).status_code == 303
    path = tmp / "watches" / "malaga-bob.yaml"
    config = load_config(path, require_secrets=False)
    assert config.notify == ("anna", "main") and config.api_key_env == "SERPAPI_KEY_ANNA"
    home = c.get("/").text
    assert "Anna, Principal (moi)" in home
    # bot utilise : suppression refusee ; cle utilisee : idem
    assert "encore utilis" in c.post("/bots/anna/delete").text
    assert "encore utilis" in c.post("/keys/SERPAPI_KEY_ANNA/delete").text
    # decocher tous les bots est refuse
    r = c.post("/watches/malaga-bob", data=_watch_form(notify=[]))
    assert r.status_code == 400 and "au moins un bot" in r.text
    # repasser au seul bot principal
    c.post("/watches/malaga-bob", data=_watch_form(notify=["main"]))
    assert load_config(path, require_secrets=False).notify == ("main",)
    assert c.post("/bots/anna/delete", follow_redirects=False).status_code == 303


def test_existing_alert_file_untouched_by_notify(ui):
    c, tmp, _ = ui
    santiago = tmp / "watches" / "santiago.yaml"
    before = santiago.read_bytes()
    form = _watch_form(destination_name="Santiago", destination="SCL", departure_center="2027-01-15",
                       departure_tolerance_days="3", duration_center_days="35", duration_tolerance_days="5",
                       required_stops="1", travel_class="2", api_key_env="SEARCH_SERPAPI_KEY", notify=["main"])
    if not load_config(santiago, require_secrets=False).enabled:
        form.pop("enabled")
    assert c.post("/watches/santiago", data=form, follow_redirects=False).status_code == 303
    assert santiago.read_bytes() == before


def test_add_key_checked_and_listed(ui):
    c, tmp, _ = ui
    r = c.post("/keys/new", data={"suffix": "bob", "value": "b" * 64}, follow_redirects=False)
    assert r.status_code == 303
    assert secret_store.read_store(tmp / "secrets")["serpapi_keys"]["SERPAPI_KEY_BOB"] == "b" * 64
    assert "SERPAPI_KEY_BOB" in c.get("/watches/new").text  # choisissable tout de suite
    assert "b" * 64 not in c.get("/bots").text
    dup = c.post("/keys/new", data={"suffix": "BOB", "value": "c" * 64})
    assert dup.status_code == 400 and "existe déjà" in dup.text


def test_deal_veille_notify(ui):
    c, tmp, _ = ui
    from webui.app import flightdeals_form_values
    from flightdeals.config import load_config as load_fd
    fd = tmp / "config" / "config.yaml"
    before = fd.read_bytes()
    values = flightdeals_form_values(load_fd(fd, require_secrets=False))
    assert c.post("/deals/main", data={**values, "notify_shown": "1", "notify": ["main"]}, follow_redirects=False).status_code == 303
    assert fd.read_bytes() == before  # bot principal seul : fichier intact
    c.post("/deals/main", data={**values, "notify_shown": "1", "notify": ["main", "anna"]})
    assert load_fd(fd, require_secrets=False).notify == ("main", "anna")
