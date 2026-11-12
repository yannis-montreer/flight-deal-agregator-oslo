"""Interface : veilles "bons plans" multiples (creation, edition, pause, suppression, cles).
Travaille sur des COPIES des fichiers du depot. Aucun appel reseau."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flightdeals import multi
from flightdeals.config import load_config as load_fd
from webui.app import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBUI_KEY_QUOTAS", raising=False)
    for d in ("config", "watches", "deals", "status"):
        (tmp_path / d).mkdir()
    main = tmp_path / "config" / "config.yaml"
    shutil.copy(REPO_ROOT / "config" / "config.yaml", main)
    shutil.copy(REPO_ROOT / "watches" / "santiago.yaml", tmp_path / "watches" / "santiago.yaml")
    (tmp_path / "status" / "keys.json").write_text(json.dumps({"updated_at": "2026-11-12T12:00:00+00:00", "keys": [
        {"name": n, "searches_per_month": 250, "searches_left": 240, "error": None}
        for n in ("SEARCH_SERPAPI_KEY", "SERPAPI_KEY", "SERPAPI_KEY_3")
    ]}), encoding="utf-8")
    client = TestClient(create_app(main, tmp_path / "watches", status_dir=tmp_path / "status",
                                   deals_dir=tmp_path / "deals"), base_url="http://127.0.0.1:8765")
    return client, main, tmp_path / "deals"


def _form(**overrides):
    form = {
        "name": "Italie printemps", "api_key_env": "SERPAPI_KEY_3", "enabled": "on",
        "region": "Italie", "month": "4", "travel_durations": ["2"],
        "min_trip_length_nights": "4", "max_trip_length_nights": "10", "minimum_discount": "30",
        "schedule": "11:00", "monthly_budget": "200", "cold_start_enabled": "on",
        "max_candidates_per_run": "2", "daily_summary_enabled": "on",
    }
    form.update(overrides)
    return form


def test_create_veille_inherits_main_settings_and_runs_with_its_own_key_and_db(env):
    client, main, deals = env
    response = client.post("/deals/new", data=_form(), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/?saved=italie-printemps"
    path = deals / "italie-printemps.yaml"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Veille creee depuis l'interface")
    assert 'name: "Italie printemps"' in text and "api_key_env: SERPAPI_KEY_3" in text
    assert "db_file: flightdeals_italie-printemps.db" in text
    assert 'arrival_area_id: "/m/03rjj"' in text and "month: 4" in text
    config = load_fd(path, require_secrets=False)
    assert config.scoring.weights == load_fd(main, require_secrets=False).scoring.weights  # seuils avances herites
    # Le service flightdeals la prendra avec sa propre base.
    configs, errors = multi.load_veilles(main, deals, require_secrets=False)
    assert set(configs) == {"main", "italie-printemps"} and errors == {}
    assert multi.db_path_for(configs["italie-printemps"], Path("/data"), Path("/data/flightdeals.db")).name == \
        "flightdeals_italie-printemps.db"
    # Le fichier principal n'a pas bouge.
    assert (REPO_ROOT / "config" / "config.yaml").read_bytes() == main.read_bytes()
    page = client.get("/").text
    assert "Italie printemps" in page and "Italie" in page


def test_create_requires_a_name_and_a_known_key(env):
    client, _, deals = env
    assert client.post("/deals/new", data=_form(name="")).status_code == 400
    assert client.post("/deals/new", data=_form(api_key_env="SERPAPI_KEY_9")).status_code == 400
    assert list(deals.glob("*.yaml")) == []


def test_create_refused_when_key_would_overflow(env):
    client, _, deals = env
    # SERPAPI_KEY porte deja la veille principale (186/250) ; 3 durees + 3 verifs = 186 de plus.
    response = client.post("/deals/new", data=_form(api_key_env="SERPAPI_KEY", travel_durations=["1", "2", "3"],
                                                     max_candidates_per_run="3"))
    assert response.status_code == 400 and "quota" in response.text
    assert list(deals.glob("*.yaml")) == []


def test_new_form_defaults_to_freest_key_and_empty_region(env):
    client, _, _ = env
    page = client.get("/deals/new").text
    assert '<option value="SERPAPI_KEY_3" selected>' in page and 'name="region" value=""' in page


def test_main_edit_without_name_keeps_file_historical(env):
    client, main, _ = env
    form = _form(name="", api_key_env="SERPAPI_KEY", region="Asie du Sud-Est", month="11",
                 travel_durations=["1", "2", "3"], min_trip_length_nights="6", max_trip_length_nights="14",
                 schedule="10:30", max_candidates_per_run="3")
    before = main.read_bytes()
    assert client.post("/deals/main", data=form, follow_redirects=False).status_code == 303
    assert main.read_bytes() == before  # aucun champ "name"/"api_key_env" ajoute inutilement


def test_main_can_be_named_then_unnamed(env):
    client, main, _ = env
    base = dict(api_key_env="SERPAPI_KEY", region="Asie du Sud-Est", month="11", travel_durations=["1", "2", "3"],
                min_trip_length_nights="6", max_trip_length_nights="14", schedule="10:30", max_candidates_per_run="3")
    client.post("/deals/main", data=_form(name="Asie", **base), follow_redirects=False)
    assert load_fd(main, require_secrets=False).name == "Asie"
    client.post("/deals/main", data=_form(name="", **base), follow_redirects=False)
    assert load_fd(main, require_secrets=False).name is None


def test_pause_main_costs_nothing(env):
    client, main, _ = env
    base = dict(name="", api_key_env="SERPAPI_KEY", region="Asie du Sud-Est", month="11",
                travel_durations=["1", "2", "3"], min_trip_length_nights="6", max_trip_length_nights="14",
                schedule="10:30", max_candidates_per_run="3")
    assert client.post("/deals/main", data=_form(enabled="", **base), follow_redirects=False).status_code == 303
    assert load_fd(main, require_secrets=False).collection.enabled is False
    page = client.get("/").text
    assert "en pause" in page and ": 0 / 250 réservées" in page


def test_delete_archives_veille_but_main_cannot_be_deleted(env):
    client, main, deals = env
    client.post("/deals/new", data=_form(), follow_redirects=False)
    assert "data/flightdeals_italie-printemps.db" in client.get("/deals/italie-printemps/delete").text
    assert client.post("/deals/italie-printemps/delete", follow_redirects=False).status_code == 303
    assert not (deals / "italie-printemps.yaml").exists()
    assert len(list((deals / ".archive").glob("italie-printemps-*.yaml"))) == 1
    assert client.post("/deals/main/delete").status_code == 400 and main.exists()


def test_legacy_url_redirects_and_unknown_ids_404(env):
    client, _, _ = env
    assert client.get("/flightdeals", follow_redirects=False).headers["location"] == "/deals/main"
    assert client.get("/deals/inconnue").status_code == 404
    assert client.get("/deals/..%2Fconfig").status_code == 404
