"""Application web (FastAPI) d'edition des veilles. Volontairement petite : 3 pages, des
formulaires HTML classiques, aucun JavaScript necessaire."""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from flightdeals.config import ConfigError as FlightDealsConfigError
from flightdeals.config import load_config as load_flightdeals_config
from trip_watch.config import ConfigError as TripWatchConfigError
from trip_watch.config import load_config as load_trip_watch_config
from trip_watch.tracker import departure_dates

from webui import costs as cost_mod
from webui import projection as proj_mod
from webui.currencies import CURRENCIES
from trip_watch.config import DEFAULT_API_KEY_ENV, ENV_NAME_RE
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from webui.store import (
    archive_document, create_document, dump_document, load_document, save_document, set_list, set_value,
)

MAIN_DEAL_ID = "main"
DEFAULT_DEAL_KEY_ENV = "SERPAPI_KEY"

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
WATCH_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")

MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
             "septembre", "octobre", "novembre", "décembre"]
TRAVEL_CLASSES = {1: "Economy", 2: "Premium Economy", 3: "Affaires", 4: "Première"}
TRAVEL_DURATIONS = {1: "Week-end", 2: "~1 semaine", 3: "~2 semaines"}

# Pays et grandes regions -> identifiant Google (voir build_regions.py, source Wikidata).
REGIONS: list[dict] = json.loads((Path(__file__).parent / "regions.json").read_text(encoding="utf-8"))
REGION_NAME_BY_ID = {r["id"]: r["name"] for r in REGIONS}
REGION_ID_BY_NAME = {r["name"].casefold(): r["id"] for r in REGIONS}
VERIFIED_AREA_IDS = {"/m/073q1"}  # verifie par un vrai appel SerpApi (voir config/config.yaml)
RAW_ID_RE = re.compile(r"^/[mg]/[0-9a-z_]+$")

# Aeroports a vols reguliers [code, ville, nom, pays] (voir build_airports.py, source OurAirports).
AIRPORTS: list[list[str]] = json.loads((Path(__file__).parent / "airports.json").read_text(encoding="utf-8"))
AIRPORT_BY_CODE = {a[0]: a for a in AIRPORTS}


def airport_label(code: str) -> str:
    airport = AIRPORT_BY_CODE.get(code)
    if not airport:
        return code
    _, city, name, country = airport
    return f"{name} — {city}, {country}" if city else f"{name} — {country}"


def airport_city(code: str) -> str:
    airport = AIRPORT_BY_CODE.get(code)
    return (airport[1] or airport[2]) if airport else ""


def region_label(area_id: Optional[str]) -> str:
    if not area_id:
        return "Partout"
    return REGION_NAME_BY_ID.get(area_id, area_id)


def parse_region(value: str) -> Optional[str]:
    """Nom choisi dans la liste -> identifiant Google ; un identifiant brut (/m/... ou /g/...)
    reste accepte pour un cas non couvert par la liste ; vide = partout."""
    value = (value or "").strip()
    if not value or value.casefold() == "partout":
        return None
    area_id = REGION_ID_BY_NAME.get(value.casefold())
    if area_id:
        return area_id
    if RAW_ID_RE.match(value):
        return value
    raise FormError(f"Région « {value} » inconnue : choisis un pays ou une région dans la liste proposée.")


def month_year(month: int, today: Optional[date] = None) -> int:
    """Annee du prochain mois `month` a venir (le mois en cours compte encore pour cette annee).
    SerpApi ne prend qu'un mois (1-12), sans annee : il cherche dans les mois a venir."""
    today = today or date.today()
    return today.year if month >= today.month else today.year + 1


def month_label(month: Optional[int], today: Optional[date] = None) -> str:
    if month is None:
        return "6 prochains mois"
    return f"{MONTHS_FR[month - 1]} {month_year(month, today)}"


CONFIG_ERRORS = (FlightDealsConfigError, TripWatchConfigError)


class FormError(Exception):
    pass


@dataclass
class WatchView:
    watch_id: str
    path: Path
    config: Any = None
    error: Optional[str] = None
    cost: Optional[cost_mod.WatchCost] = None
    warnings: tuple = ()


def _int(form: dict, name: str, label: str, minimum: int = 0, maximum: int = 100000) -> int:
    raw = (form.get(name) or "").strip()
    if not re.fullmatch(r"-?\d+", raw):
        raise FormError(f"{label} : nombre entier attendu.")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise FormError(f"{label} : doit être entre {minimum} et {maximum}.")
    return value


def _text(form: dict, name: str, label: str) -> str:
    value = (form.get(name) or "").strip()
    if not value:
        raise FormError(f"{label} : obligatoire.")
    return value


def _iata(form: dict, name: str, label: str) -> str:
    value = _text(form, name, label).upper()
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise FormError(f"{label} : tape le nom de la ville et choisis l'aéroport dans la liste (code de 3 lettres, ex : OSL).")
    if value not in AIRPORT_BY_CODE:
        raise FormError(f"{label} : le code « {value} » ne correspond à aucun aéroport à vols réguliers. "
                        "Tape le nom de la ville et choisis dans la liste.")
    return value


def slugify(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:30] or "alerte"


def new_watch_text(watch_id: str, form: dict, key_env: str) -> str:
    """Contenu d'un nouveau fichier d'alerte. Les valeurs ont deja ete controlees par
    validate_watch_form ; json.dumps produit une chaine YAML entre guillemets valide."""
    q = json.dumps
    return (
        f"# Alerte creee depuis l'interface le {date.today().isoformat()}.\n"
        f"# Modifiable depuis l'interface (http://127.0.0.1:8765).\n\n"
        f"api_key_env: {key_env}\n"
        f"enabled: {'true' if form['enabled'] else 'false'}\n"
        f"db_file: trip_watch_{watch_id}.db\n\n"
        f"destination_name: {q(form['destination_name'], ensure_ascii=False)}\n"
        f"origin: {form['origin']}\n"
        f"destination: {form['destination']}\n"
        f"currency: {form['currency']}\n"
        f"schedule: {q(form['schedule'])}\n"
        f"timezone: \"Europe/Oslo\"\n\n"
        f"departure:\n  center: {q(form['departure_center'])}\n  tolerance_days: {form['departure_tolerance_days']}\n\n"
        f"duration:\n  center_days: {form['duration_center_days']}\n  tolerance_days: {form['duration_tolerance_days']}\n\n"
        f"filters:\n  required_stops: {form['required_stops']}\n  travel_class: {form['travel_class']}\n"
    )


def create_app(
    flightdeals_path: Path, watches_dir: Path, allowed_hosts: Optional[list[str]] = None,
    status_dir: Optional[Path] = None, deals_dir: Optional[Path] = None,
) -> FastAPI:
    allowed_hosts = allowed_hosts or ["127.0.0.1", "localhost"]
    app = FastAPI(title="Veilles", docs_url=None, redoc_url=None, openapi_url=None)
    # Protection contre le DNS rebinding : seule l'adresse locale est acceptee.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def reject_cross_site_posts(request: Request, call_next):
        # Anti-CSRF : un site web ouvert dans le navigateur de la NUC ne doit pas pouvoir
        # soumettre un formulaire a cette interface (qui n'a pas de mot de passe).
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if request.headers.get("sec-fetch-site") == "cross-site" or (
                origin and urlparse(origin).hostname not in allowed_hosts
            ):
                return Response("Requete refusee (origine externe).", status_code=403)
        return await call_next(request)

    # ---------- chargement ----------

    def watch_path(watch_id: str) -> Path:
        path = watches_dir / f"{watch_id}.yaml"
        if not WATCH_ID_RE.match(watch_id) or not path.is_file():
            raise FileNotFoundError(watch_id)
        return path

    def load_watches() -> list[WatchView]:
        views = []
        for path in sorted(watches_dir.glob("*.yaml")):
            view = WatchView(path.stem, path)
            if not WATCH_ID_RE.match(path.stem):
                continue
            try:
                view.config = load_trip_watch_config(path, require_secrets=False)
                view.cost = cost_mod.trip_watch_cost(path.stem, view.config)
                view.warnings = tuple(watch_warnings(view.config))
            except CONFIG_ERRORS as exc:
                view.error = str(exc)
            views.append(view)
        return views

    def load_flightdeals() -> tuple[Any, Optional[str]]:
        try:
            return load_flightdeals_config(flightdeals_path, require_secrets=False), None
        except CONFIG_ERRORS as exc:
            return None, str(exc)

    # Veilles "bons plans" : "main" = config/config.yaml (historique), les autres = deals/<id>.yaml.
    def deal_path(veille_id: str) -> Path:
        if veille_id == MAIN_DEAL_ID:
            return flightdeals_path
        path = deals_dir / f"{veille_id}.yaml" if deals_dir is not None else None
        if path is None or not WATCH_ID_RE.match(veille_id) or not path.is_file():
            raise FileNotFoundError(veille_id)
        return path

    def load_deals() -> list[WatchView]:
        paths = [(MAIN_DEAL_ID, flightdeals_path)]
        if deals_dir is not None and deals_dir.is_dir():
            paths += [(p.stem, p) for p in sorted(deals_dir.glob("*.yaml"))
                      if WATCH_ID_RE.match(p.stem) and p.stem != MAIN_DEAL_ID]
        views = []
        for vid, path in paths:
            view = WatchView(vid, path)
            try:
                view.config = load_flightdeals_config(path, require_secrets=False)
                view.cost = cost_mod.flightdeals_cost(view.config, vid)
                view.warnings = tuple(flightdeals_warnings(view.config))
            except CONFIG_ERRORS as exc:
                view.error = str(exc)
            views.append(view)
        return views

    def read_registry() -> Optional[dict]:
        """Registre des cles publie par le service des alertes (noms + compteurs, jamais de
        secret). None s'il n'existe pas encore."""
        if status_dir is None:
            return None
        try:
            data = json.loads((status_dir / "keys.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and isinstance(data.get("keys"), list) else None
        except (OSError, ValueError):
            return None

    def key_names(registry: Optional[dict]) -> Optional[list[str]]:
        return sorted(k["name"] for k in registry["keys"] if isinstance(k.get("name"), str)) if registry else None

    def key_choices(registry: Optional[dict], current: str = "") -> list[str]:
        names = key_names(registry)
        if names is None:  # registre indisponible : cles deja utilisees + cle par defaut
            names = sorted({w.config.api_key_env for w in load_watches() + load_deals() if w.config}
                           | {DEFAULT_API_KEY_ENV})
        return sorted(set(names) | ({current} if current else set()))

    def all_costs(replace_id: Optional[str] = None, replacement: Optional[cost_mod.WatchCost] = None) -> list:
        result = [d.cost for d in load_deals() if d.cost is not None]
        result += [w.cost for w in load_watches() if w.cost is not None]
        if replace_id is not None:
            result = [c for c in result if c.watch_id != replace_id] + [replacement]
        return result

    def consumers_by_key() -> dict[str, list[proj_mod.Consumer]]:
        """Qui consomme quelle cle, a quelle heure, combien par run (alertes actives seulement)."""
        result: dict[str, list[proj_mod.Consumer]] = {}
        for d in load_deals():
            if d.config and d.config.collection.enabled:
                result.setdefault(d.cost.key_env, []).append(proj_mod.Consumer(
                    d.cost.label, d.cost.per_day, d.config.collection.schedule, d.config.collection.timezone))
        for w in load_watches():
            if w.config and w.config.enabled:
                result.setdefault(w.config.api_key_env, []).append(proj_mod.Consumer(
                    w.config.destination_name, w.cost.per_day, w.config.schedule, w.config.timezone))
        return result

    def projection_for(entry: dict, quota: int, consumers: list) -> Optional[proj_mod.Projection]:
        used, renewal = entry.get("used_this_period"), entry.get("renewal_date")
        if not isinstance(used, int) or not isinstance(renewal, str):
            return None
        try:
            renewal_date = date.fromisoformat(renewal)
        except ValueError:
            return None
        return proj_mod.project(used, quota, renewal_date, consumers, datetime.now(timezone.utc))

    def budget_rows(costs: list, registry: Optional[dict]) -> list[dict]:
        by_name = {k.get("name"): k for k in (registry or {}).get("keys", [])}
        names = sorted(set(cost_mod.totals_by_key(costs)) | set(by_name))
        consumers = consumers_by_key()
        rows = []
        for key_env in names:
            total = cost_mod.totals_by_key(costs).get(key_env, 0)
            quota = cost_mod.quota_for(key_env, registry)
            entry = by_name.get(key_env, {})
            rows.append({
                "projection": projection_for(entry, quota, consumers.get(key_env, [])),
                "key_env": key_env, "total": total, "quota": quota,
                "percent": min(100, round(100 * total / quota)) if quota else 100,
                "users": [c.label for c in costs if c.key_env == key_env and c.per_month > 0],
                "over": total > quota,
                "left": entry.get("searches_left"),
                "missing": registry is not None and key_env not in by_name,
                "error": entry.get("error"),
            })
        return rows

    def budget_check(registry, replace_id, replacement):
        cost_mod.check_budget(all_costs(replace_id, replacement), registry)

    # ---------- pages ----------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, saved: str = "", deleted: str = ""):
        watches = load_watches()
        costs = all_costs()
        registry = read_registry()
        return TEMPLATES.TemplateResponse(request, "index.html", {
            "deals": load_deals(), "deals_enabled": deals_dir is not None,
            "watches": watches, "budgets": budget_rows(costs, registry), "saved": saved, "deleted": deleted,
            "registry": registry,
            "month_label": month_label, "region_label": region_label, "classes": TRAVEL_CLASSES,
            "airport_city": airport_city,
        })

    @app.get("/flightdeals")
    def flightdeals_legacy():
        return RedirectResponse("/deals/main", status_code=303)

    def _key_names_including(registry, current: str) -> Optional[list[str]]:
        names = key_names(registry)
        return None if names is None else sorted(set(names) | {current})

    # /deals/new est declare AVANT /deals/{veille_id}.
    @app.get("/deals/new", response_class=HTMLResponse)
    def deal_new_form(request: Request):
        fd_config, fd_error = load_flightdeals()
        if fd_config is None:
            return _page_error(request, f"La veille principale est invalide (sert de modèle) : {fd_error}")
        registry = read_registry()
        values = flightdeals_form_values(fd_config)
        totals = cost_mod.totals_by_key(all_costs())
        choices = key_choices(registry)
        values.update({"name": "", "region": "", "enabled": "on",
                       "api_key_env": max(choices, key=lambda k: cost_mod.quota_for(k, registry) - totals.get(k, 0))})
        return _render_flightdeals(request, None, values, None, registry)

    @app.post("/deals/new", response_class=HTMLResponse)
    async def deal_create(request: Request):
        form_data = await request.form()
        form = dict(form_data)
        form["travel_durations"] = form_data.getlist("travel_durations")
        registry = read_registry()
        try:
            if deals_dir is None:
                raise FormError("Veilles multiples non configurées (WEBUI_DEALS_DIR absent).")
            name = _text(form, "name", "Nom de la veille")
            base = slugify(name)
            veille_id, n = base, 2
            while (deals_dir / f"{veille_id}.yaml").exists() or veille_id in (MAIN_DEAL_ID, "new"):
                veille_id, n = f"{base}-{n}", n + 1
            doc = load_document(flightdeals_path)  # modele : la veille principale (seuils avances inclus)
            apply_flightdeals_form(doc, form, key_names(registry), is_main=False)
            doc.insert(min(2, len(doc)), "db_file", f"flightdeals_{veille_id}.db")
            original = flightdeals_path.read_bytes().decode("utf-8")
            header = f"# Veille creee depuis l'interface le {date.today().isoformat()} (modele : config/config.yaml).\n"
            create_document(
                deals_dir / f"{veille_id}.yaml", header + dump_document(doc, original=original).replace("\r\n", "\n"),
                validate=lambda p: load_flightdeals_config(p, require_secrets=False),
                check=lambda cfg: budget_check(registry, cost_mod.deal_cost_id(veille_id), cost_mod.flightdeals_cost(cfg, veille_id)),
            )
        except (FormError, cost_mod.BudgetError, FileExistsError, *CONFIG_ERRORS) as exc:
            return _render_flightdeals(request, None, form, str(exc), registry, status_code=400)
        return RedirectResponse(f"/?saved={veille_id}", status_code=303)

    @app.get("/deals/{veille_id}", response_class=HTMLResponse)
    def deal_form(request: Request, veille_id: str):
        try:
            path = deal_path(veille_id)
        except FileNotFoundError:
            return _page_error(request, "Veille introuvable.", status_code=404)
        try:
            config = load_flightdeals_config(path, require_secrets=False)
        except CONFIG_ERRORS as exc:
            return _page_error(request, f"Le fichier actuel est invalide, à corriger à la main : {exc}")
        return _render_flightdeals(request, veille_id, flightdeals_form_values(config), None, read_registry())

    @app.post("/deals/{veille_id}", response_class=HTMLResponse)
    async def deal_save(request: Request, veille_id: str):
        try:
            path = deal_path(veille_id)
        except FileNotFoundError:
            return _page_error(request, "Veille introuvable.", status_code=404)
        form_data = await request.form()
        form = dict(form_data)
        form["travel_durations"] = form_data.getlist("travel_durations")
        registry = read_registry()
        try:
            current = load_flightdeals_config(path, require_secrets=False)
            doc = load_document(path)
            apply_flightdeals_form(doc, form, _key_names_including(registry, current.api_key_env),
                                   is_main=veille_id == MAIN_DEAL_ID)
            save_document(
                path, doc,
                validate=lambda p: load_flightdeals_config(p, require_secrets=False),
                check=lambda cfg: budget_check(registry, cost_mod.deal_cost_id(veille_id), cost_mod.flightdeals_cost(cfg, veille_id)),
            )
        except (FormError, cost_mod.BudgetError, *CONFIG_ERRORS) as exc:
            return _render_flightdeals(request, veille_id, form, str(exc), registry, status_code=400)
        return RedirectResponse(f"/?saved={veille_id}", status_code=303)

    @app.get("/deals/{veille_id}/delete", response_class=HTMLResponse)
    def deal_delete_confirm(request: Request, veille_id: str):
        if veille_id == MAIN_DEAL_ID:
            return _page_error(request, "La veille principale ne se supprime pas : mets-la en pause.", status_code=400)
        try:
            path = deal_path(veille_id)
        except FileNotFoundError:
            return _page_error(request, "Veille introuvable.", status_code=404)
        try:
            config = load_flightdeals_config(path, require_secrets=False)
            label, db_file = cost_mod.deal_label(config), config.db_file
        except CONFIG_ERRORS:
            label, db_file = veille_id, None
        return TEMPLATES.TemplateResponse(request, "delete.html", {
            "watch_id": veille_id, "label": label, "db_file": db_file, "kind": "deals"})

    @app.post("/deals/{veille_id}/delete", response_class=HTMLResponse)
    def deal_delete(request: Request, veille_id: str):
        if veille_id == MAIN_DEAL_ID:
            return _page_error(request, "La veille principale ne se supprime pas : mets-la en pause.", status_code=400)
        try:
            path = deal_path(veille_id)
        except FileNotFoundError:
            return _page_error(request, "Veille introuvable.", status_code=404)
        archive_document(path)
        return RedirectResponse(f"/?deleted={veille_id}", status_code=303)

    # /watches/new est declare AVANT /watches/{watch_id} (sinon "new" serait pris pour un nom).
    @app.get("/watches/new", response_class=HTMLResponse)
    def watch_new_form(request: Request):
        registry = read_registry()
        choices = key_choices(registry)
        # Cle proposee par defaut : celle qui a le plus de marge ce mois-ci.
        totals = cost_mod.totals_by_key(all_costs())
        default_key = max(choices, key=lambda k: cost_mod.quota_for(k, registry) - totals.get(k, 0)) if choices else DEFAULT_API_KEY_ENV
        return _render_watch(request, None, new_watch_defaults(default_key), None, registry)

    @app.post("/watches/new", response_class=HTMLResponse)
    async def watch_create(request: Request):
        form = dict(await request.form())
        registry = read_registry()
        try:
            values = parse_watch_form(form, key_names(registry))
            base = slugify(values["destination_name"])
            watch_id, n = base, 2
            while (watches_dir / f"{watch_id}.yaml").exists() or watch_id == "new":
                watch_id, n = f"{base}-{n}", n + 1
            create_document(
                watches_dir / f"{watch_id}.yaml", new_watch_text(watch_id, values, values["api_key_env"]),
                validate=lambda p: load_trip_watch_config(p, require_secrets=False),
                check=lambda cfg: budget_check(registry, watch_id, cost_mod.trip_watch_cost(watch_id, cfg)),
            )
        except (FormError, cost_mod.BudgetError, FileExistsError, *CONFIG_ERRORS) as exc:
            return _render_watch(request, None, form, str(exc), registry, status_code=400)
        return RedirectResponse(f"/?saved={watch_id}", status_code=303)

    @app.get("/watches/{watch_id}", response_class=HTMLResponse)
    def watch_form(request: Request, watch_id: str):
        try:
            path = watch_path(watch_id)
        except FileNotFoundError:
            return _page_error(request, "Alerte introuvable.", status_code=404)
        try:
            config = load_trip_watch_config(path, require_secrets=False)
        except CONFIG_ERRORS as exc:
            return _page_error(request, f"Le fichier actuel est invalide, à corriger à la main : {exc}")
        return _render_watch(request, watch_id, watch_form_values(config), None, read_registry())

    @app.post("/watches/{watch_id}", response_class=HTMLResponse)
    async def watch_save(request: Request, watch_id: str):
        try:
            path = watch_path(watch_id)
        except FileNotFoundError:
            return _page_error(request, "Alerte introuvable.", status_code=404)
        form = dict(await request.form())
        registry = read_registry()
        try:
            current = load_trip_watch_config(path, require_secrets=False)
            names = key_names(registry)
            if names is not None and current.api_key_env not in names:
                names = names + [current.api_key_env]  # garder la cle actuelle reste possible
            values = parse_watch_form(form, names)
            doc = load_document(path)
            apply_watch_form(doc, values)
            save_document(
                path, doc, validate=lambda p: load_trip_watch_config(p, require_secrets=False),
                check=lambda cfg: budget_check(registry, watch_id, cost_mod.trip_watch_cost(watch_id, cfg)),
            )
        except (FormError, cost_mod.BudgetError, *CONFIG_ERRORS) as exc:
            return _render_watch(request, watch_id, form, str(exc), registry, status_code=400)
        return RedirectResponse(f"/?saved={watch_id}", status_code=303)

    @app.get("/watches/{watch_id}/delete", response_class=HTMLResponse)
    def watch_delete_confirm(request: Request, watch_id: str):
        try:
            path = watch_path(watch_id)
        except FileNotFoundError:
            return _page_error(request, "Alerte introuvable.", status_code=404)
        try:
            config = load_trip_watch_config(path, require_secrets=False)
            label, db_file = config.destination_name, config.db_file
        except CONFIG_ERRORS:
            label, db_file = watch_id, None
        return TEMPLATES.TemplateResponse(request, "delete.html", {
            "watch_id": watch_id, "label": label, "db_file": db_file, "kind": "watches"})

    @app.post("/watches/{watch_id}/delete", response_class=HTMLResponse)
    def watch_delete(request: Request, watch_id: str):
        try:
            path = watch_path(watch_id)
        except FileNotFoundError:
            return _page_error(request, "Alerte introuvable.", status_code=404)
        archive_document(path)
        return RedirectResponse(f"/?deleted={watch_id}", status_code=303)

    def _key_options(registry, current_key):
        totals = cost_mod.totals_by_key(all_costs())
        return [{"name": k, "quota": cost_mod.quota_for(k, registry), "used": totals.get(k, 0)}
                for k in key_choices(registry, current_key)]

    def _render_flightdeals(request, veille_id, values, error, registry, status_code=200):
        return TEMPLATES.TemplateResponse(request, "flightdeals.html", {
            "veille_id": veille_id, "is_new": veille_id is None, "is_main": veille_id == MAIN_DEAL_ID,
            "key_options": _key_options(registry, values.get("api_key_env", "")),
            "registry_ok": registry is not None, "deals_enabled": deals_dir is not None,
            "v": values, "error": error, "durations": TRAVEL_DURATIONS,
            # Ordre chronologique a partir du mois en cours (novembre 2026, decembre 2026, janvier 2027...).
            "month_options": [
                (m, month_label(m)) for m in sorted(range(1, 13), key=lambda m: (month_year(m), m))
            ],
            "regions": REGIONS,
        }, status_code=status_code)

    def _render_watch(request, watch_id, values, error, registry, status_code=200):
        current_key = values.get("api_key_env", "")
        choices = key_choices(registry, current_key)
        totals = cost_mod.totals_by_key(all_costs())
        key_options = [
            {"name": k, "quota": cost_mod.quota_for(k, registry), "used": totals.get(k, 0)} for k in choices
        ]
        return TEMPLATES.TemplateResponse(request, "watch.html", {
            "watch_id": watch_id, "is_new": watch_id is None, "v": values, "error": error,
            "classes": TRAVEL_CLASSES, "key_options": key_options, "registry_ok": registry is not None,
            "airports": AIRPORTS, "airport_label": airport_label, "currencies": CURRENCIES,
        }, status_code=status_code)

    def _page_error(request, message, status_code=500):
        return TEMPLATES.TemplateResponse(request, "error.html", {"message": message}, status_code=status_code)
    return app


# ---------- conversions formulaire <-> fichier ----------

def flightdeals_form_values(config) -> dict:
    return {
        "name": config.name or "",
        "api_key_env": config.api_key_env,
        "enabled": "on" if config.collection.enabled else "",
        "schedule": config.collection.schedule,
        "month": "" if config.serpapi.month is None else str(config.serpapi.month),
        "region": region_label(config.serpapi.arrival_area_id) if config.serpapi.arrival_area_id else "",
        "travel_durations": [str(d) for d in config.serpapi.travel_durations],
        "min_trip_length_nights": str(config.deal.min_trip_length_nights),
        "max_trip_length_nights": str(config.deal.max_trip_length_nights),
        "minimum_discount": str(round(config.deal.minimum_discount * 100)),
        "monthly_budget": str(config.serpapi.monthly_budget),
        "cold_start_enabled": "on" if config.cold_start_check.enabled else "",
        "max_candidates_per_run": str(config.cold_start_check.max_candidates_per_run),
        "daily_summary_enabled": "on" if config.notification.daily_summary_enabled else "",
    }


def apply_flightdeals_form(doc, form: dict, key_names: Optional[list[str]] = None, *, is_main: bool = True) -> None:
    """`key_names` : cles presentes dans .env (registre) ; None = registre indisponible.
    `is_main` : veille historique (nom facultatif ; sans nom, ses messages restent inchanges)."""
    name = (form.get("name") or "").strip()
    if not is_main and not name:
        raise FormError("Nom de la veille : obligatoire (il apparaît dans les messages Telegram).")
    if len(name) > 60:
        raise FormError("Nom de la veille : 60 caractères maximum.")
    key_env = (form.get("api_key_env") or "").strip() or DEFAULT_DEAL_KEY_ENV
    if not ENV_NAME_RE.match(key_env):
        raise FormError("Clé SerpApi : nom de variable invalide.")
    if key_names is not None and key_env not in key_names:
        raise FormError(f"Clé SerpApi : « {key_env} » n'est pas présente dans le fichier .env de la NUC.")
    enabled = bool(form.get("enabled"))
    schedule = _text(form, "schedule", "Heure du run")
    month_raw = (form.get("month") or "").strip()
    month = None if month_raw == "" else _int(form, "month", "Mois", 1, 12)
    area = parse_region(form.get("region") or "")
    durations = sorted({_int({"d": d}, "d", "Durées", 1, 3) for d in form.get("travel_durations") or []})
    if not durations:
        raise FormError("Durées de séjour : coche au moins une case.")
    min_n = _int(form, "min_trip_length_nights", "Séjour minimum (nuits)", 0, 365)
    max_n = _int(form, "max_trip_length_nights", "Séjour maximum (nuits)", 0, 365)
    discount = _int(form, "minimum_discount", "Baisse minimale (%)", 1, 99)
    budget = _int(form, "monthly_budget", "Budget mensuel", 1, 100000)
    cold_enabled = bool(form.get("cold_start_enabled"))
    cold_max = _int(form, "max_candidates_per_run", "Vérifications Google max", 0, 20)
    summary = bool(form.get("daily_summary_enabled"))

    # Champs propres aux veilles multiples : ajoutes en tete de fichier s'ils n'existent pas
    # encore, et jamais ajoutes inutilement a la veille principale (fichier historique intact).
    for key, value, quoted in (("name", name or None, True), ("api_key_env", key_env, False)):
        if key in doc:
            if value is None:
                del doc[key]
            else:
                set_value(doc, key, value, quoted=quoted)
        elif value is not None and not (key == "api_key_env" and value == DEFAULT_DEAL_KEY_ENV and is_main):
            doc.insert(0, key, DoubleQuotedScalarString(value) if quoted else value)
    set_value(doc["collection"], "enabled", enabled)
    set_value(doc["collection"], "schedule", schedule, quoted=True)
    set_value(doc["serpapi"], "month", month)
    set_value(doc["serpapi"], "arrival_area_id", area, quoted=True)
    set_list(doc["serpapi"], "travel_durations", durations)
    set_value(doc["serpapi"], "monthly_budget", budget)
    set_value(doc["deal"], "min_trip_length_nights", min_n)
    set_value(doc["deal"], "max_trip_length_nights", max_n)
    set_value(doc["deal"], "minimum_discount", round(discount / 100, 2))
    set_value(doc["notification"], "daily_summary_enabled", summary)
    if "cold_start_check" in doc:
        set_value(doc["cold_start_check"], "enabled", cold_enabled)
        set_value(doc["cold_start_check"], "max_candidates_per_run", cold_max)


def watch_form_values(config) -> dict:
    return {
        "api_key_env": config.api_key_env,
        "enabled": "on" if config.enabled else "",
        "destination_name": config.destination_name,
        "origin": config.origin,
        "destination": config.destination,
        "currency": config.currency,
        "schedule": config.schedule,
        "departure_center": config.departure_center.isoformat(),
        "departure_tolerance_days": str(config.departure_tolerance_days),
        "duration_center_days": str(config.duration_center_days),
        "duration_tolerance_days": str(config.duration_tolerance_days),
        "required_stops": str(config.required_stops),
        "travel_class": str(config.travel_class),
    }


def new_watch_defaults(key_env: str, today: Optional[date] = None) -> dict:
    today = today or date.today()
    return {
        "api_key_env": key_env, "enabled": "on", "destination_name": "", "origin": "OSL", "destination": "",
        "currency": "NOK", "schedule": "10:30", "departure_center": (today + timedelta(days=60)).isoformat(),
        "departure_tolerance_days": "1", "duration_center_days": "14", "duration_tolerance_days": "2",
        "required_stops": "1", "travel_class": "1",
    }


def parse_watch_form(form: dict, key_names: Optional[list[str]]) -> dict:
    """Controle et normalise les champs d'une alerte. `key_names` : cles connues du service
    des alertes (registre) ; None = registre indisponible, on ne controle que le format."""
    center_raw = _text(form, "departure_center", "Date de départ centrale")
    try:
        date.fromisoformat(center_raw)
    except ValueError as exc:
        raise FormError("Date de départ centrale : format AAAA-MM-JJ attendu.") from exc
    currency = _text(form, "currency", "Devise").upper()
    if currency not in CURRENCIES:
        raise FormError(f"Devise : « {currency} » n'est pas dans la liste. Tape le nom (ex : « euro ») et choisis.")
    key_env = _text(form, "api_key_env", "Clé SerpApi")
    if not ENV_NAME_RE.match(key_env):
        raise FormError("Clé SerpApi : nom de variable invalide.")
    if key_names is not None and key_env not in key_names:
        raise FormError(f"Clé SerpApi : « {key_env} » n'est pas présente dans le fichier .env de la NUC.")
    schedule = _text(form, "schedule", "Heure du run")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", schedule):
        raise FormError("Heure du run : format HH:MM attendu.")
    return {
        "api_key_env": key_env,
        "enabled": bool(form.get("enabled")),
        "destination_name": _text(form, "destination_name", "Nom de l'alerte"),
        "origin": _iata(form, "origin", "Départ"),
        "destination": _iata(form, "destination", "Arrivée"),
        "currency": currency,
        "schedule": schedule,
        "departure_center": center_raw,
        "departure_tolerance_days": _int(form, "departure_tolerance_days", "Tolérance départ (jours)", 0, 30),
        "duration_center_days": _int(form, "duration_center_days", "Durée du séjour (jours)", 1, 365),
        "duration_tolerance_days": _int(form, "duration_tolerance_days", "Tolérance durée (jours)", 0, 60),
        "required_stops": _int(form, "required_stops", "Escales", 0, 3),
        "travel_class": _int(form, "travel_class", "Classe", 1, 4),
    }


def apply_watch_form(doc, values: dict) -> None:
    set_value(doc, "api_key_env", values["api_key_env"])
    set_value(doc, "enabled", values["enabled"])
    set_value(doc, "destination_name", values["destination_name"])
    set_value(doc, "origin", values["origin"])
    set_value(doc, "destination", values["destination"])
    set_value(doc, "currency", values["currency"])
    set_value(doc, "schedule", values["schedule"], quoted=True)
    set_value(doc["departure"], "center", values["departure_center"], quoted=True)
    set_value(doc["departure"], "tolerance_days", values["departure_tolerance_days"])
    set_value(doc["duration"], "center_days", values["duration_center_days"])
    set_value(doc["duration"], "tolerance_days", values["duration_tolerance_days"])
    set_value(doc["filters"], "required_stops", values["required_stops"])
    set_value(doc["filters"], "travel_class", values["travel_class"])

# ---------- avertissements (non bloquants) ----------

def flightdeals_warnings(config, today: Optional[date] = None) -> list[str]:
    today = today or date.today()
    warnings = []
    area_id = config.serpapi.arrival_area_id
    if area_id and area_id not in VERIFIED_AREA_IDS:
        warnings.append(
            f"La zone « {region_label(area_id)} » n'a encore jamais été testée avec cette recherche : "
            "vérifie que le récap Telegram du lendemain contient bien des vols."
        )
    month = config.serpapi.month
    if month is not None and month == today.month:
        warnings.append(f"Le mois surveillé ({month_label(month, today)}) est en cours : pense à choisir le suivant.")
    elif month is not None and month < today.month:
        warnings.append(
            f"{month_label(month, today).capitalize()} est un mois de l'année prochaine : cas jamais observé "
            "jusqu'ici, vérifie les dates des vols dans le récap Telegram du lendemain."
        )
    return warnings


def watch_warnings(config, today: Optional[date] = None) -> list[str]:
    today = today or date.today()
    dates = departure_dates(config.departure_center, config.departure_tolerance_days)
    if dates[-1] < today:
        return ["Toutes les dates de départ sont passées : cette veille ne sert plus à rien."]
    if dates[0] < today:
        return ["Une partie des dates de départ est déjà passée."]
    return []


def create_app_from_env() -> FastAPI:
    return create_app(
        Path(os.environ.get("WEBUI_FLIGHTDEALS_CONFIG", "config/config.yaml")),
        Path(os.environ.get("WEBUI_WATCHES_DIR", "watches")),
        deals_dir=Path(os.environ["WEBUI_DEALS_DIR"]) if os.environ.get("WEBUI_DEALS_DIR") else None,
        status_dir=Path(os.environ["WEBUI_STATUS_DIR"]) if os.environ.get("WEBUI_STATUS_DIR") else None,
    )
