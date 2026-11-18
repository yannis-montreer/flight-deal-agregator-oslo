"""Interface en français ou en anglais : aucune phrase oubliée, bascule par cookie."""
from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

import jinja2
import pytest
from fastapi.testclient import TestClient
from jinja2 import nodes

from webui import app as app_mod
from webui import bots as bots_mod
from webui.i18n import EN, Msg, tr
from webui.app import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent
WEBUI = REPO_ROOT / "webui"


def template_keys() -> set[str]:
    env = jinja2.Environment()
    keys = set()
    for path in (WEBUI / "templates").glob("*.html"):
        tree = env.parse(path.read_text(encoding="utf-8"))
        for call in tree.find_all(nodes.Call):
            if isinstance(call.node, nodes.Name) and call.node.name == "_" and call.args and isinstance(call.args[0], nodes.Const):
                keys.add(call.args[0].value)
    return keys


def python_keys() -> set[str]:
    keys = set()
    for path in WEBUI.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                continue
            if call.func.id == "Msg" and call.args:
                value = call.args[0]
                if isinstance(value, ast.Constant):
                    keys.add(value.value)
                elif isinstance(value, ast.Name) and value.id == "template":  # costs.flightdeals_cost
                    pass
            if call.func.id in ("_int", "_text", "_iata") and len(call.args) >= 3 and isinstance(call.args[2], ast.Constant):
                keys.add(call.args[2].value)
    # libelles affiches via _(variable) ou tr(variable)
    keys |= set(app_mod.TRAVEL_CLASSES.values()) | set(app_mod.STOPS_OPTIONS.values()) | set(app_mod.TRAVEL_DURATIONS.values())
    keys |= set(bots_mod.MESSAGES.values()) | set(bots_mod.ERRORS.values())
    keys |= {"Principal (moi)", "dans .env",
             "{explore} recherche(s) d'exploration/jour",
             "{explore} recherche(s) d'exploration/jour + jusqu'à {cold} vérification(s) Google (démarrage à froid)",
             "{explore} recherche(s) d'exploration/jour ; plafonné par le budget interne de {budget}/mois (des runs seront sautés en fin de mois)",
             "{explore} recherche(s) d'exploration/jour + jusqu'à {cold} vérification(s) Google (démarrage à froid) ; plafonné par le budget interne de {budget}/mois (des runs seront sautés en fin de mois)"}
    return keys


def test_every_text_has_an_english_translation():
    missing = sorted((template_keys() | python_keys()) - set(EN))
    assert not missing, "Traductions manquantes :\n" + "\n".join(missing)


def test_placeholders_match():
    import string
    for fr, en in EN.items():
        names = lambda s: sorted(f for _, f, _, _ in string.Formatter().parse(s) if f)
        assert names(fr) == names(en), (fr, en)


def test_msg_translation():
    assert tr(Msg("{label} : obligatoire.", label="Heure du run"), "en") == "Run time: required."
    assert tr(Msg("{label} : obligatoire.", label="Heure du run"), "fr") == "Heure du run : obligatoire."


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBUI_KEY_QUOTAS", raising=False)
    for d in ("config", "watches", "status", "secrets"):
        (tmp_path / d).mkdir()
    shutil.copy(REPO_ROOT / "config" / "config.yaml", tmp_path / "config" / "config.yaml")
    shutil.copy(REPO_ROOT / "watches" / "santiago.yaml", tmp_path / "watches" / "santiago.yaml")
    (tmp_path / "status" / "keys.json").write_text(json.dumps({"updated_at": "2026-11-18T10:00:00", "keys": [
        {"name": n, "searches_per_month": 250} for n in ("SEARCH_SERPAPI_KEY", "SERPAPI_KEY")]}), encoding="utf-8")
    return TestClient(create_app(tmp_path / "config" / "config.yaml", tmp_path / "watches",
                                 status_dir=tmp_path / "status", secrets_dir=tmp_path / "secrets",
                                 deals_dir=tmp_path / "deals"), base_url="http://127.0.0.1")


def test_switch_language_by_cookie(client):
    assert "Alertes trajet" in client.get("/").text and 'href="/lang/en' in client.get("/").text
    r = client.get("/lang/en?next=/watches/new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/watches/new"
    for page in ("/", "/watches/new", "/watches/santiago", "/deals/main", "/deals/new", "/bots", "/bots/new", "/keys/new"):
        text = client.get(page).text
        assert 'lang="en"' in text, page
        for french in ("Annuler", "Enregistrer", "Alertes trajet", "Clé SerpApi", "Retour"):
            assert french not in text, (page, french)
    assert "Trip alerts" in client.get("/").text
    client.get("/lang/fr")
    assert "Alertes trajet" in client.get("/").text


def test_form_errors_translated(client):
    client.get("/lang/en")
    form = {"destination_name": "X", "origin": "OSL", "destination": "ZZZ", "departure_center": "2027-03-10",
            "departure_tolerance_days": "1", "duration_center_days": "7", "duration_tolerance_days": "1",
            "required_stops": "1", "travel_class": "1", "currency": "NOK", "schedule": "10:30",
            "api_key_env": "SERPAPI_KEY", "enabled": "on"}
    text = client.post("/watches/new", data=form).text
    assert "does not match any airport" in text


def test_open_redirect_refused(client):
    assert client.get("/lang/en?next=//evil.example", follow_redirects=False).headers["location"] == "/"
    assert client.get("/lang/en?next=https://evil.example", follow_redirects=False).headers["location"] == "/"
