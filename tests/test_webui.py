"""Interface d'edition : travaille sur des COPIES des vrais fichiers du depot (tmp_path),
jamais sur les originaux. Aucun appel reseau."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from flightdeals.config import load_config as load_fd
from trip_watch.config import load_config as load_tw
from webui.app import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBUI_KEY_QUOTAS", raising=False)
    config_dir = tmp_path / "config"
    watches_dir = tmp_path / "watches"
    config_dir.mkdir()
    watches_dir.mkdir()
    fd_path = config_dir / "config.yaml"
    shutil.copy(REPO_ROOT / "config" / "config.yaml", fd_path)
    shutil.copy(REPO_ROOT / "watches" / "santiago.yaml", watches_dir / "santiago.yaml")
    client = TestClient(create_app(fd_path, watches_dir), base_url="http://127.0.0.1:8765")
    return client, fd_path, watches_dir


def _fd_form(**overrides):
    form = {
        "region": "Asie du Sud-Est", "month": "11", "travel_durations": ["1", "2", "3"],
        "min_trip_length_nights": "6", "max_trip_length_nights": "14", "minimum_discount": "30",
        "schedule": "10:30", "monthly_budget": "200", "cold_start_enabled": "on",
        "max_candidates_per_run": "3", "daily_summary_enabled": "on",
    }
    form.update(overrides)
    return form


def _watch_form(**overrides):
    form = {
        "destination_name": "Santiago", "origin": "OSL", "destination": "SCL", "departure_center": "2027-01-15",
        "departure_tolerance_days": "3", "duration_center_days": "35", "duration_tolerance_days": "5",
        "required_stops": "1", "travel_class": "2", "currency": "NOK", "schedule": "10:30",
    }
    form.update(overrides)
    return form


def _history(directory: Path) -> list:
    return list((directory / ".history").glob("*.yaml")) if (directory / ".history").exists() else []


def test_index_lists_both_watches_and_budgets(env):
    client, _, _ = env
    response = client.get("/")
    assert response.status_code == 200
    assert "Santiago" in response.text
    assert "Asie du Sud-Est" in response.text
    assert "SEARCH_SERPAPI_KEY" in response.text and "SERPAPI_KEY" in response.text
    assert "217 / 250" in response.text  # 7 dates x 31 jours


def test_forms_render(env):
    client, _, _ = env
    assert client.get("/flightdeals").status_code == 200
    page = client.get("/watches/santiago")
    assert page.status_code == 200 and 'value="2027-01-15"' in page.text


def test_unchanged_save_writes_nothing(env):
    client, fd_path, watches_dir = env
    before = fd_path.read_bytes()
    # Formulaire construit depuis le fichier tel qu'il est (config/config.yaml est edite en
    # production via l'interface : ses valeurs ne sont pas figees).
    from webui.app import flightdeals_form_values
    current = flightdeals_form_values(load_fd(fd_path, require_secrets=False))
    response = client.post("/flightdeals", data=current, follow_redirects=False)
    assert response.status_code == 303
    assert fd_path.read_bytes() == before
    assert _history(fd_path.parent) == []


def test_flightdeals_save_keeps_comments_quotes_and_backup(env):
    client, fd_path, _ = env
    original = fd_path.read_text(encoding="utf-8")
    response = client.post(
        "/flightdeals", data=_fd_form(month="12", travel_durations=["2", "3"], schedule="09:15"), follow_redirects=False
    )
    assert response.status_code == 303
    text = fd_path.read_text(encoding="utf-8")
    assert "month: 12" in text
    assert "travel_durations: [2, 3]" in text
    assert 'schedule: "09:15"' in text
    # Les commentaires (nombreux et precieux dans ce fichier) survivent a l'edition.
    assert "# Angle mort assume du MVP (spec)" in text
    assert "weights: { discount: 0.45" in text
    assert len([l for l in text.splitlines() if l.strip().startswith("#")]) == len(
        [l for l in original.splitlines() if l.strip().startswith("#")]
    )
    config = load_fd(fd_path, require_secrets=False)
    assert config.serpapi.month == 12 and config.collection.schedule == "09:15"
    backups = _history(fd_path.parent)
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == original


def test_flightdeals_everywhere_and_all_months(env):
    client, fd_path, _ = env
    client.post("/flightdeals", data=_fd_form(month="", region=""), follow_redirects=False)
    raw = yaml.safe_load(fd_path.read_text(encoding="utf-8"))
    assert raw["serpapi"]["month"] is None and raw["serpapi"]["arrival_area_id"] is None


def test_watch_save(env):
    client, _, watches_dir = env
    path = watches_dir / "santiago.yaml"
    response = client.post(
        "/watches/santiago", data=_watch_form(departure_center="2027-02-01", destination="lim", travel_class="3"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    text = path.read_text(encoding="utf-8")
    assert 'center: "2027-02-01"' in text and "destination: LIM" in text
    assert "api_key_env: SEARCH_SERPAPI_KEY" in text
    config = load_tw(path, require_secrets=False)
    assert config.travel_class == 3 and config.destination == "LIM"
    assert len(_history(watches_dir)) == 1


def test_api_key_env_cannot_be_changed_from_form(env):
    client, _, watches_dir = env
    client.post("/watches/santiago", data=_watch_form(api_key_env="SERPAPI_KEY", destination_name="X"))
    assert load_tw(watches_dir / "santiago.yaml", require_secrets=False).api_key_env == "SEARCH_SERPAPI_KEY"


def test_budget_overrun_is_refused_and_file_untouched(env):
    client, _, watches_dir = env
    path = watches_dir / "santiago.yaml"
    before = path.read_bytes()
    response = client.post("/watches/santiago", data=_watch_form(departure_tolerance_days="10"))
    assert response.status_code == 400
    assert "quota" in response.text
    assert path.read_bytes() == before
    assert _history(watches_dir) == []


def test_paid_quota_allows_more(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setenv("WEBUI_KEY_QUOTAS", "SEARCH_SERPAPI_KEY=1000")
    response = client.post("/watches/santiago", data=_watch_form(departure_tolerance_days="10"), follow_redirects=False)
    assert response.status_code == 303


def test_shared_key_is_counted_once_per_watch(env):
    """Une 2e veille sur la meme cle : la somme des deux est controlee."""
    client, _, watches_dir = env
    shutil.copy(watches_dir / "santiago.yaml", watches_dir / "lima.yaml")
    assert "434 / 250" in client.get("/").text
    response = client.post("/watches/santiago", data=_watch_form(destination_name="Autre"))
    assert response.status_code == 400


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"origin": "OS"}, "3 lettres"),
        ({"destination": "LYO"}, "aucun aéroport"),
        ({"departure_center": "15/01/2027"}, "AAAA-MM-JJ"),
        ({"duration_center_days": "3", "duration_tolerance_days": "5"}, "center_days"),
        ({"schedule": "25:00"}, "schedule"),
        ({"departure_tolerance_days": "abc"}, "entier"),
    ],
)
def test_invalid_watch_input_rejected(env, overrides, message):
    client, _, watches_dir = env
    before = (watches_dir / "santiago.yaml").read_bytes()
    response = client.post("/watches/santiago", data=_watch_form(**overrides))
    assert response.status_code == 400
    assert message in response.text
    assert (watches_dir / "santiago.yaml").read_bytes() == before


def test_invalid_flightdeals_input_rejected(env):
    client, fd_path, _ = env
    before = fd_path.read_bytes()
    assert client.post("/flightdeals", data=_fd_form(travel_durations=[])).status_code == 400
    assert client.post("/flightdeals", data=_fd_form(min_trip_length_nights="20")).status_code == 400
    assert fd_path.read_bytes() == before


def test_cross_site_post_refused(env):
    client, fd_path, _ = env
    before = fd_path.read_bytes()
    response = client.post("/flightdeals", data=_fd_form(month="12"), headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    response = client.post("/flightdeals", data=_fd_form(month="12"), headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert fd_path.read_bytes() == before


def test_same_origin_post_accepted(env):
    client, _, _ = env
    response = client.post(
        "/flightdeals", data=_fd_form(), headers={"Origin": "http://localhost:8765"}, follow_redirects=False
    )
    assert response.status_code == 303


def test_foreign_host_refused(env):
    client, _, _ = env
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


@pytest.mark.parametrize("watch_id", ["inconnue", "..%2Fconfig%2Fconfig", "Santiago"])
def test_unknown_or_unsafe_watch_ids(env, watch_id):
    client, _, _ = env
    assert client.get(f"/watches/{watch_id}").status_code == 404


# ---------- destination (liste pays/regions) et mois avec annee ----------

def test_region_chosen_by_name_is_stored_as_google_id(env):
    client, fd_path, _ = env
    response = client.post("/flightdeals", data=_fd_form(region="italie"), follow_redirects=False)
    assert response.status_code == 303
    assert 'arrival_area_id: "/m/03rjj"' in fd_path.read_text(encoding="utf-8")
    page = client.get("/").text
    assert "Italie" in page and "jamais été testée" in page
    assert 'value="Italie"' in client.get("/flightdeals").text


def test_region_raw_id_still_accepted(env):
    client, fd_path, _ = env
    client.post("/flightdeals", data=_fd_form(region="/g/11abc_def"), follow_redirects=False)
    assert 'arrival_area_id: "/g/11abc_def"' in fd_path.read_text(encoding="utf-8")


def test_unknown_region_rejected(env):
    client, fd_path, _ = env
    before = fd_path.read_bytes()
    response = client.post("/flightdeals", data=_fd_form(region="Atlantide"))
    assert response.status_code == 400 and "inconnue" in response.text
    assert fd_path.read_bytes() == before


def test_current_region_shown_by_name(env):
    client, _, _ = env
    assert 'value="Asie du Sud-Est"' in client.get("/flightdeals").text


def test_regions_file_is_consistent():
    from webui.app import REGIONS
    names = [r["name"] for r in REGIONS]
    ids = [r["id"] for r in REGIONS]
    assert len(names) == len(set(names)) and len(ids) == len(set(ids))
    assert {"Italie", "Norvège", "Chili", "Asie du Sud-Est", "Pays-Bas", "Danemark"} <= set(names)
    assert all(i.startswith("/m/") for i in ids)


def test_month_labels_carry_the_year():
    from datetime import date
    from webui.app import month_label
    today = date(2026, 11, 12)
    assert month_label(1, today) == "janvier 2027"
    assert month_label(11, today) == "novembre 2026"
    assert month_label(12, today) == "décembre 2026"
    assert month_label(None, today) == "6 prochains mois"


def test_month_select_shows_years(env):
    client, _, _ = env
    page = client.get("/flightdeals").text
    from datetime import date
    year = date.today().year
    assert f"{year}<" in page or f"{year + 1}<" in page


# ---------- autocompletion des aeroports ----------

def test_airport_codes_validated_against_list(env):
    client, _, watches_dir = env
    response = client.post("/watches/santiago", data=_watch_form(destination="lys"), follow_redirects=False)
    assert response.status_code == 303
    assert "destination: LYS" in (watches_dir / "santiago.yaml").read_text(encoding="utf-8")
    assert "LYS (Colombier-Saugnieu" in client.get("/").text


def test_watch_form_offers_airport_list(env):
    client, _, _ = env
    page = client.get("/watches/santiago").text
    assert 'list="airports"' in page
    assert '<option value="LYS">Lyon Saint-Exupéry Airport' in page
    assert "Oslo-Gardermoen International Airport" in page  # libelle sous le champ Depart


def test_airports_file_is_consistent():
    from webui.app import AIRPORTS
    codes = [a[0] for a in AIRPORTS]
    assert len(codes) == len(set(codes)) > 3000
    assert {"OSL", "SCL", "LYS", "CDG", "BKK", "LIM", "LPB"} <= set(codes)
    assert all(len(a) == 4 and len(a[0]) == 3 for a in AIRPORTS)