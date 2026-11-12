"""Chargement/validation de la config trip_watch (cle par veille, mode sans secrets)."""
from __future__ import annotations

from pathlib import Path

import pytest

from trip_watch.config import ConfigError, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE = """\
{api_key_line}
origin: OSL
destination: SCL
destination_name: Santiago
currency: NOK
schedule: "10:30"
timezone: "Europe/Oslo"
departure:
  center: "2027-01-15"
  tolerance_days: 3
duration:
  center_days: 35
  tolerance_days: 5
filters:
  required_stops: 1
  travel_class: 2
"""


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("SEARCH_SERPAPI_KEY", "SERPAPI_KEY", "SERPAPI_KEY_3", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")


def _write(tmp_path, api_key_line="", **replacements) -> Path:
    text = BASE.format(api_key_line=api_key_line)
    for old, new in replacements.items():
        text = text.replace(old, new)
    path = tmp_path / "watch.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_repo_santiago_file_is_valid_without_secrets():
    config = load_config(REPO_ROOT / "watches" / "santiago.yaml", require_secrets=False)
    assert config.destination == "SCL"
    assert config.api_key_env == "SEARCH_SERPAPI_KEY"


def test_default_key_is_search_key_with_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("SERPAPI_KEY", "main")
    config = load_config(_write(tmp_path))
    assert config.api_key == "main"
    assert config.api_key_env == "SEARCH_SERPAPI_KEY"
    monkeypatch.setenv("SEARCH_SERPAPI_KEY", "search")
    assert load_config(_write(tmp_path)).api_key == "search"


def test_api_key_env_selects_dedicated_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_SERPAPI_KEY", "search")
    monkeypatch.setenv("SERPAPI_KEY_3", "third")
    config = load_config(_write(tmp_path, "api_key_env: SERPAPI_KEY_3"))
    assert config.api_key == "third"
    assert config.api_key_env == "SERPAPI_KEY_3"


def test_api_key_env_missing_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCH_SERPAPI_KEY", "search")
    with pytest.raises(ConfigError, match="SERPAPI_KEY_3"):
        load_config(_write(tmp_path, "api_key_env: SERPAPI_KEY_3"))


def test_api_key_env_must_be_a_variable_name(tmp_path):
    with pytest.raises(ConfigError, match="api_key_env"):
        load_config(_write(tmp_path, "api_key_env: 'abc-123 secret'"), require_secrets=False)


def test_require_secrets_false_skips_secret_checks(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    config = load_config(_write(tmp_path), require_secrets=False)
    assert config.api_key == ""


@pytest.mark.parametrize(
    "old,new,message",
    [
        ("origin: OSL", "origin: oslo", "origin"),
        ("destination: SCL", "destination: SC", "destination"),
        ('schedule: "10:30"', 'schedule: "25:00"', "schedule"),
        ("tolerance_days: 3", "tolerance_days: -1", "departure_tolerance_days"),
        ("center_days: 35", "center_days: 4", "center_days"),
        ('center: "2027-01-15"', 'center: "15/01/2027"', "Valeur invalide"),
        ("destination_name: Santiago", "destination_name: ''", "destination_name"),
    ],
)
def test_invalid_values_rejected(tmp_path, old, new, message):
    with pytest.raises(ConfigError, match=message):
        load_config(_write(tmp_path, **{old: new}), require_secrets=False)
