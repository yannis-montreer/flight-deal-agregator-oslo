"""Mode d'escales "au plus N" (stops_rule: at_most) : filtre, config, interface. Aucun appel reseau."""
from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trip_watch.config import ConfigError, load_config
from trip_watch.tracker import _search_one, stops_label, stops_match
from webui.app import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent


def _flight(price, legs):
    return {"price": price, "flights": [{"airline": "X"}] * legs}


class _Client:
    def __init__(self, response):
        self.response = response

    def search(self, params):
        return self.response


@pytest.mark.parametrize("stops,required,rule,expected", [
    (0, 1, "exact", False), (1, 1, "exact", True), (2, 1, "exact", False),
    (0, 1, "at_most", True), (1, 1, "at_most", True), (2, 1, "at_most", False),
])
def test_stops_match(stops, required, rule, expected):
    assert stops_match(stops, required, rule) is expected


def test_at_most_keeps_cheapest_among_direct_and_one_stop():
    data = {"best_flights": [_flight(2500, 1)], "other_flights": [_flight(1900, 2), _flight(1500, 3)]}
    common = dict(origin="OSL", destination="AGP", outbound_date=date(2026, 12, 25), return_date=date(2027, 1, 3),
                  currency="NOK", required_stops=1, travel_class=1)
    best = _search_one(_Client(data), stops_rule="at_most", **common)
    assert best.price == 1900 and best.stops == 1          # 1 escale moins chere que le direct, 2 escales exclu
    exact_direct = _search_one(_Client(data), **{**common, "required_stops": 0})
    assert exact_direct.price == 2500 and exact_direct.stops == 0
    none = _search_one(_Client({"best_flights": [_flight(900, 4)]}), stops_rule="at_most", **common)
    assert none.price is None and "direct ou jusqu'a 1 escale" in none.error


def test_labels():
    assert stops_label(1, "at_most") == "direct ou jusqu'a 1 escale(s)"
    assert stops_label(1, "exact") == "1 escale(s)"


def test_config_reads_and_validates_stops_rule(tmp_path):
    text = (REPO_ROOT / "watches" / "santiago.yaml").read_text(encoding="utf-8")
    assert load_config(REPO_ROOT / "watches" / "santiago.yaml", require_secrets=False).stops_rule == "exact"
    ok = tmp_path / "ok.yaml"
    ok.write_text(text.replace("  travel_class: 2", "  travel_class: 2\n  stops_rule: at_most"), encoding="utf-8")
    assert load_config(ok, require_secrets=False).stops_rule == "at_most"
    bad = tmp_path / "bad.yaml"
    bad.write_text(text.replace("  travel_class: 2", "  travel_class: 2\n  stops_rule: whatever"), encoding="utf-8")
    with pytest.raises(ConfigError, match="stops_rule"):
        load_config(bad, require_secrets=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBUI_KEY_QUOTAS", raising=False)
    for d in ("config", "watches", "status"):
        (tmp_path / d).mkdir()
    shutil.copy(REPO_ROOT / "config" / "config.yaml", tmp_path / "config" / "config.yaml")
    shutil.copy(REPO_ROOT / "watches" / "santiago.yaml", tmp_path / "watches" / "santiago.yaml")
    (tmp_path / "status" / "keys.json").write_text(json.dumps({"updated_at": "x", "keys": [
        {"name": n, "searches_per_month": 250} for n in ("SEARCH_SERPAPI_KEY", "SERPAPI_KEY", "SERPAPI_KEY_3")]}),
        encoding="utf-8")
    c = TestClient(create_app(tmp_path / "config" / "config.yaml", tmp_path / "watches",
                              status_dir=tmp_path / "status"), base_url="http://127.0.0.1")
    return c, tmp_path / "watches"


def _form(**overrides):
    form = {"destination_name": "Malaga", "origin": "OSL", "destination": "AGP", "departure_center": "2026-12-25",
            "departure_tolerance_days": "1", "duration_center_days": "9", "duration_tolerance_days": "1",
            "required_stops": "max1", "travel_class": "1", "currency": "NOK", "schedule": "10:30",
            "api_key_env": "SERPAPI_KEY_3", "enabled": "on"}
    form.update(overrides)
    return form


def test_create_alert_direct_or_one_stop(client):
    c, watches = client
    assert c.post("/watches/new", data=_form(), follow_redirects=False).status_code == 303
    config = load_config(watches / "malaga.yaml", require_secrets=False)
    assert (config.required_stops, config.stops_rule) == (1, "at_most")
    assert "Direct ou 1 escale" in c.get("/").text
    assert '<option value="max1" selected>' in c.get("/watches/malaga").text


def test_existing_exact_alert_file_untouched_and_switchable(client):
    c, watches = client
    santiago = watches / "santiago.yaml"
    santiago_form = _form(destination_name="Santiago", destination="SCL", departure_center="2027-01-15",
                          departure_tolerance_days="3", duration_center_days="35", duration_tolerance_days="5",
                          required_stops="1", travel_class="2", api_key_env="SEARCH_SERPAPI_KEY")
    before = santiago.read_bytes()
    assert c.post("/watches/santiago", data=santiago_form, follow_redirects=False).status_code == 303
    assert santiago.read_bytes() == before  # pas de stops_rule ajoute pour rien
    c.post("/watches/santiago", data={**santiago_form, "required_stops": "max1"}, follow_redirects=False)
    assert load_config(santiago, require_secrets=False).stops_rule == "at_most"


def test_unknown_stops_value_refused(client):
    c, watches = client
    assert c.post("/watches/new", data=_form(required_stops="max9")).status_code == 400
    assert not (watches / "malaga.yaml").exists()
