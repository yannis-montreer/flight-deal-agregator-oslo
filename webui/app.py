"""Application web (FastAPI) d'edition des veilles. Volontairement petite : 3 pages, des
formulaires HTML classiques, aucun JavaScript necessaire."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
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
from webui.store import load_document, save_document, set_list, set_value

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
WATCH_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")

MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
             "septembre", "octobre", "novembre", "décembre"]
TRAVEL_CLASSES = {1: "Economy", 2: "Premium Economy", 3: "Affaires", 4: "Première"}
TRAVEL_DURATIONS = {1: "Week-end", 2: "~1 semaine", 3: "~2 semaines"}
KNOWN_AREAS = {"/m/073q1": "Asie du Sud-Est"}

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
        raise FormError(f"{label} : code IATA de 3 lettres attendu (ex : OSL).")
    return value


def create_app(flightdeals_path: Path, watches_dir: Path, allowed_hosts: Optional[list[str]] = None) -> FastAPI:
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

    def all_costs(replace_id: Optional[str] = None, replacement: Optional[cost_mod.WatchCost] = None) -> list:
        result = []
        fd_config, _ = load_flightdeals()
        if fd_config is not None:
            result.append(cost_mod.flightdeals_cost(fd_config))
        result += [w.cost for w in load_watches() if w.cost is not None]
        if replace_id is not None:
            result = [c for c in result if c.watch_id != replace_id] + [replacement]
        return result

    def budget_rows(costs: list) -> list[dict]:
        rows = []
        for key_env, total in sorted(cost_mod.totals_by_key(costs).items()):
            quota = cost_mod.quota_for(key_env)
            rows.append({
                "key_env": key_env, "total": total, "quota": quota,
                "percent": min(100, round(100 * total / quota)) if quota else 100,
                "users": [c.label for c in costs if c.key_env == key_env],
                "over": total > quota,
            })
        return rows

    # ---------- pages ----------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, saved: str = ""):
        fd_config, fd_error = load_flightdeals()
        watches = load_watches()
        costs = all_costs()
        return TEMPLATES.TemplateResponse(request, "index.html", {
            "fd": fd_config, "fd_error": fd_error,
            "fd_cost": cost_mod.flightdeals_cost(fd_config) if fd_config else None,
            "fd_warnings": flightdeals_warnings(fd_config) if fd_config else [],
            "watches": watches, "budgets": budget_rows(costs), "saved": saved,
            "months": MONTHS_FR, "classes": TRAVEL_CLASSES, "areas": KNOWN_AREAS,
        })

    @app.get("/flightdeals", response_class=HTMLResponse)
    def flightdeals_form(request: Request):
        fd_config, fd_error = load_flightdeals()
        if fd_config is None:
            return _page_error(request, f"Le fichier actuel est invalide, à corriger à la main : {fd_error}")
        return _render_flightdeals(request, flightdeals_form_values(fd_config), None)

    @app.post("/flightdeals", response_class=HTMLResponse)
    async def flightdeals_save(request: Request):
        form_data = await request.form()
        form = dict(form_data)
        form["travel_durations"] = form_data.getlist("travel_durations")
        try:
            doc = load_document(flightdeals_path)
            apply_flightdeals_form(doc, form)

            def check(cfg):
                cost_mod.check_budget(all_costs("flightdeals", cost_mod.flightdeals_cost(cfg)))

            save_document(
                flightdeals_path, doc,
                validate=lambda p: load_flightdeals_config(p, require_secrets=False), check=check,
            )
        except (FormError, cost_mod.BudgetError, *CONFIG_ERRORS) as exc:
            return _render_flightdeals(request, form, str(exc), status_code=400)
        return RedirectResponse("/?saved=flightdeals", status_code=303)

    @app.get("/watches/{watch_id}", response_class=HTMLResponse)
    def watch_form(request: Request, watch_id: str):
        try:
            path = watch_path(watch_id)
        except FileNotFoundError:
            return _page_error(request, "Veille introuvable.", status_code=404)
        try:
            config = load_trip_watch_config(path, require_secrets=False)
        except CONFIG_ERRORS as exc:
            return _page_error(request, f"Le fichier actuel est invalide, à corriger à la main : {exc}")
        return _render_watch(request, watch_id, watch_form_values(config), None)

    @app.post("/watches/{watch_id}", response_class=HTMLResponse)
    async def watch_save(request: Request, watch_id: str):
        try:
            path = watch_path(watch_id)
        except FileNotFoundError:
            return _page_error(request, "Veille introuvable.", status_code=404)
        form = dict(await request.form())
        try:
            current = load_trip_watch_config(path, require_secrets=False)
            form["api_key_env"] = current.api_key_env  # non modifiable depuis l'interface
            doc = load_document(path)
            apply_watch_form(doc, form)

            def check(cfg):
                cost_mod.check_budget(all_costs(watch_id, cost_mod.trip_watch_cost(watch_id, cfg)))

            save_document(path, doc, validate=lambda p: load_trip_watch_config(p, require_secrets=False), check=check)
        except (FormError, cost_mod.BudgetError, *CONFIG_ERRORS) as exc:
            return _render_watch(request, watch_id, form, str(exc), status_code=400)
        return RedirectResponse(f"/?saved={watch_id}", status_code=303)

    def _render_flightdeals(request, values, error, status_code=200):
        return TEMPLATES.TemplateResponse(request, "flightdeals.html", {
            "v": values, "error": error, "months": MONTHS_FR, "durations": TRAVEL_DURATIONS, "areas": KNOWN_AREAS,
        }, status_code=status_code)

    def _render_watch(request, watch_id, values, error, status_code=200):
        return TEMPLATES.TemplateResponse(request, "watch.html", {
            "watch_id": watch_id, "v": values, "error": error, "classes": TRAVEL_CLASSES,
            "quota": cost_mod.quota_for(values.get("api_key_env", "")),
        }, status_code=status_code)

    def _page_error(request, message, status_code=500):
        return TEMPLATES.TemplateResponse(request, "error.html", {"message": message}, status_code=status_code)

    return app


# ---------- conversions formulaire <-> fichier ----------

def flightdeals_form_values(config) -> dict:
    return {
        "schedule": config.collection.schedule,
        "month": "" if config.serpapi.month is None else str(config.serpapi.month),
        "arrival_area_id": config.serpapi.arrival_area_id or "",
        "travel_durations": [str(d) for d in config.serpapi.travel_durations],
        "min_trip_length_nights": str(config.deal.min_trip_length_nights),
        "max_trip_length_nights": str(config.deal.max_trip_length_nights),
        "minimum_discount": str(round(config.deal.minimum_discount * 100)),
        "monthly_budget": str(config.serpapi.monthly_budget),
        "cold_start_enabled": "on" if config.cold_start_check.enabled else "",
        "max_candidates_per_run": str(config.cold_start_check.max_candidates_per_run),
        "daily_summary_enabled": "on" if config.notification.daily_summary_enabled else "",
    }


def apply_flightdeals_form(doc, form: dict) -> None:
    schedule = _text(form, "schedule", "Heure du run")
    month_raw = (form.get("month") or "").strip()
    month = None if month_raw == "" else _int(form, "month", "Mois", 1, 12)
    area = (form.get("arrival_area_id") or "").strip() or None
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


def apply_watch_form(doc, form: dict) -> None:
    center_raw = _text(form, "departure_center", "Date de départ centrale")
    try:
        date.fromisoformat(center_raw)
    except ValueError as exc:
        raise FormError("Date de départ centrale : format AAAA-MM-JJ attendu.") from exc

    set_value(doc, "destination_name", _text(form, "destination_name", "Nom de la veille"))
    set_value(doc, "origin", _iata(form, "origin", "Départ"))
    set_value(doc, "destination", _iata(form, "destination", "Arrivée"))
    set_value(doc, "currency", _text(form, "currency", "Devise").upper())
    set_value(doc, "schedule", _text(form, "schedule", "Heure du run"), quoted=True)
    set_value(doc["departure"], "center", center_raw, quoted=True)
    set_value(doc["departure"], "tolerance_days", _int(form, "departure_tolerance_days", "Tolérance départ (jours)", 0, 30))
    set_value(doc["duration"], "center_days", _int(form, "duration_center_days", "Durée du séjour (jours)", 1, 365))
    set_value(doc["duration"], "tolerance_days", _int(form, "duration_tolerance_days", "Tolérance durée (jours)", 0, 60))
    set_value(doc["filters"], "required_stops", _int(form, "required_stops", "Escales", 0, 3))
    set_value(doc["filters"], "travel_class", _int(form, "travel_class", "Classe", 1, 4))


# ---------- avertissements (non bloquants) ----------

def flightdeals_warnings(config, today: Optional[date] = None) -> list[str]:
    today = today or date.today()
    month = config.serpapi.month
    if month is None:
        return []
    if month < today.month:
        return [f"Le mois surveillé ({MONTHS_FR[month - 1]}) est passé cette année : "
                "le comportement de SerpApi dans ce cas n'a jamais été vérifié."]
    if month == today.month:
        return [f"Le mois surveillé ({MONTHS_FR[month - 1]}) est en cours : pense à choisir le suivant."]
    return []


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
    )
