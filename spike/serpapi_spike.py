"""
Spike jetable — validation de la source de donnees SerpApi pour le Flight Deal Aggregator OSL.

Objectif (plan, jalon M1) : mesurer si `google_travel_explore` et `google_flights` renvoient
des donnees OSL exploitables (destinations, prix, dates, structure) dans le budget cible,
AVANT d'ecrire la moindre ligne de code applicatif (db/collectors/analysis).

Usage:
    python spike/serpapi_spike.py

Necessite SERPAPI_KEY dans l'environnement (ou un fichier .env a la racine du repo, charge
automatiquement via python-dotenv si present). Ce script n'importe RIEN de flightdeals/ —
il est volontairement autonome et jetable, appele a etre modifie/relance sans contrainte de
retro-compatibilite.

Chaque appel HTTP a SerpApi consomme 1 requete du quota mensuel, succes ou echec. Ce script
en consomme au maximum 5 par execution (3x google_travel_explore + 2x google_flights).

IMPORTANT : les noms de champs et parametres ci-dessous (destination_airport, flight_price,
type=1 pour round-trip, etc.) sont bases sur la documentation SerpApi au moment de l'ecriture
de ce script mais SerpApi expose un scrape non contractuel de l'UI Google Flights — c'est
exactement pour verifier empiriquement ces hypotheses que ce spike existe. Toute erreur ou
champ manquant doit etre note dans SPIKE_NOTES.md plutot que de faire planter le script.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass  # python-dotenv est une commodite de dev local ; en Docker/CI les env vars sont deja injectees

SERPAPI_BASE_URL = "https://serpapi.com/search.json"
RESPONSES_DIR = Path(__file__).parent / "responses"
NOTES_PATH = Path(__file__).parent / "SPIKE_NOTES.md"

ORIGIN = "OSL"
CURRENCY = "NOK"
TRAVEL_DURATIONS = {1: "weekend", 2: "~1 semaine", 3: "~2 semaines"}
# Destinations candidates pour l'echantillon point-a-point google_flights (mode A).
# Choisies arbitrairement : Tokyo = exemple du spec produit, Bangkok = long-courrier populaire.
GOOGLE_FLIGHTS_SAMPLE_DESTINATIONS = ["NRT", "BKK"]

# Seuils de la conclusion GO/NO-GO SUGGEREE (voir plan, jalon M1) — a confirmer manuellement.
MIN_EXPLOITABLE_POINTS = 15
MAX_REQUESTS_BUDGET = 20


class SpikeError(Exception):
    pass


def _api_key() -> str:
    key = os.environ.get("SERPAPI_KEY", "").strip()
    if not key:
        raise SpikeError(
            "SERPAPI_KEY manquant. Cree un compte gratuit sur "
            "https://serpapi.com/manage-api-key puis mets la cle dans un fichier .env "
            "a la racine (voir .env.example) ou exporte-la dans l'environnement."
        )
    return key


def _call_serpapi(params: dict, request_log: list) -> dict:
    """Point d'appel HTTP unique -> point de comptage unique des requetes consommees."""
    full_params = {**params, "api_key": _api_key()}
    request_log.append(dict(params))  # log sans la cle API
    response = httpx.get(SERPAPI_BASE_URL, params=full_params, timeout=30.0)
    response.raise_for_status()
    return response.json()


def _save_raw(name: str, data: dict) -> Path:
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESPONSES_DIR / f"{timestamp}_{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _sample_dates() -> tuple[str, str]:
    """Dates arbitraires ~45 jours dans le futur, sejour de 7 nuits (coherent avec l'exemple du spec)."""
    outbound = datetime.now(timezone.utc).date() + timedelta(days=45)
    return_ = outbound + timedelta(days=7)
    return outbound.isoformat(), return_.isoformat()


def explore_destinations(travel_duration: int, request_log: list) -> dict:
    label = TRAVEL_DURATIONS[travel_duration]
    print(f"\n--- google_travel_explore | departure_id={ORIGIN} | travel_duration={travel_duration} ({label}) ---")
    params = {
        "engine": "google_travel_explore",
        "departure_id": ORIGIN,
        "travel_duration": travel_duration,
        "currency": CURRENCY,
    }
    try:
        data = _call_serpapi(params, request_log)
    except httpx.HTTPStatusError as exc:
        print(f"  ERREUR {exc.response.status_code}: {exc.response.text[:300]}")
        return {"travel_duration": travel_duration, "error": str(exc), "destinations_count": 0, "exploitable_count": 0}

    saved_to = _save_raw(f"explore_duration{travel_duration}", data)
    destinations = data.get("destinations", [])
    print(f"  {len(destinations)} destination(s) retournee(s). JSON brut -> {saved_to}")

    for d in destinations[:10]:  # apercu des 10 premieres seulement, le JSON complet est sauvegarde
        airport = d.get("destination_airport", "?")
        name = d.get("name", "?")
        price = d.get("flight_price")
        start = d.get("start_date", "?")
        stops = d.get("number_of_stops")
        airline = d.get("airline", "?")
        print(f"    {airport!s:5s} {name!s:20s} {start!s:12s} price={price!s:8s} stops={stops!s:4s} airline={airline}")

    exploitable_count = sum(1 for d in destinations if d.get("flight_price") is not None)
    print(f"  => {exploitable_count}/{len(destinations)} avec un prix exploitable.")
    return {
        "travel_duration": travel_duration,
        "destinations_count": len(destinations),
        "exploitable_count": exploitable_count,
        "raw_path": str(saved_to),
    }


def google_flights_sample(destination: str, request_log: list) -> dict:
    print(f"\n--- google_flights | {ORIGIN} -> {destination} ---")
    outbound_date, return_date = _sample_dates()
    params = {
        "engine": "google_flights",
        "departure_id": ORIGIN,
        "arrival_id": destination,
        "outbound_date": outbound_date,
        "return_date": return_date,
        "type": "1",  # 1=round trip selon la doc SerpApi au moment de l'ecriture — a verifier si erreur 400
        "currency": CURRENCY,
    }
    try:
        data = _call_serpapi(params, request_log)
    except httpx.HTTPStatusError as exc:
        print(f"  ERREUR {exc.response.status_code}: {exc.response.text[:300]}")
        return {"destination": destination, "error": str(exc), "itineraries_count": 0}

    saved_to = _save_raw(f"flights_{destination}", data)
    best = data.get("best_flights") or data.get("other_flights") or []
    print(f"  {len(best)} itineraire(s) retourne(s). JSON brut -> {saved_to}")
    if best:
        top = best[0]
        legs = top.get("flights", [])
        print(f"    prix={top.get('price')} duree_totale={top.get('total_duration')}min escales={max(len(legs) - 1, 0)}")
    return {"destination": destination, "itineraries_count": len(best), "raw_path": str(saved_to)}


def main() -> int:
    request_log: list = []
    explore_results = []
    flights_results = []

    try:
        _api_key()  # echoue tot et clairement si la cle manque, avant le moindre appel HTTP
    except SpikeError as exc:
        print(f"\nERREUR DE CONFIGURATION: {exc}", file=sys.stderr)
        return 1

    for duration in sorted(TRAVEL_DURATIONS):
        explore_results.append(explore_destinations(duration, request_log))

    for dest in GOOGLE_FLIGHTS_SAMPLE_DESTINATIONS:
        flights_results.append(google_flights_sample(dest, request_log))

    total_requests = len(request_log)
    total_destinations = sum(r.get("destinations_count", 0) for r in explore_results)
    total_exploitable = sum(r.get("exploitable_count", 0) for r in explore_results)
    total_exploitable += sum(r.get("itineraries_count", 0) for r in flights_results)

    suggested = (
        "GO"
        if total_exploitable >= MIN_EXPLOITABLE_POINTS and total_requests <= MAX_REQUESTS_BUDGET
        else "NO-GO (a verifier manuellement — voir les erreurs/limites ci-dessus et dans responses/)"
    )

    lines = [
        "# Spike SerpApi — Flight Deal Aggregator OSL",
        "",
        f"Execute le : {datetime.now(timezone.utc).isoformat()}",
        "",
        f"- **API utilisee** : SerpApi (`google_travel_explore` pour departure_id={ORIGIN}, "
        f"`google_flights` en echantillon sur {GOOGLE_FLIGHTS_SAMPLE_DESTINATIONS})",
        f"- **Nombre de requetes consommees** : {total_requests}",
        f"- **Nombre de destinations retournees (explore, cumule sur les 3 durees)** : {total_destinations}",
        f"- **Nombre de prix exploitables (cumule)** : {total_exploitable}",
        "- **Structure des donnees** : voir les fichiers JSON bruts dans `spike/responses/`",
        "- **Limites constatees** : _a completer manuellement apres inspection des JSON_ "
        "(ex: return_date absent ? destinations \"partout\" instables d'un jour sur l'autre ? "
        "champs manquants/renommes ?)",
        f"- **Cout estime** : {total_requests} requetes pour ce spike ; en regime de croisiere, "
        "3 requetes/jour (explore x3 durees) = ~90/mois, sous le budget cible de 200/mois.",
        f"- **Conclusion suggeree (a confirmer manuellement)** : {suggested}",
        f"  _(seuil : >= {MIN_EXPLOITABLE_POINTS} points exploitables pour <= {MAX_REQUESTS_BUDGET} "
        "requetes. Avant de valider un GO definitif, relancer ce script un autre jour et comparer "
        "les destinations retournees pour verifier qu'elles se recoupent suffisamment (sinon les "
        "buckets n'atteindront jamais 7 observations, voir risques dans le plan)._",
        "",
        "## Detail par appel",
    ]
    for r in explore_results:
        if "error" in r:
            lines.append(f"- explore travel_duration={r['travel_duration']}: ERREUR — {r['error']}")
        else:
            lines.append(
                f"- explore travel_duration={r['travel_duration']}: {r['destinations_count']} destinations, "
                f"{r['exploitable_count']} exploitables, JSON: `{r.get('raw_path', 'N/A')}`"
            )
    for r in flights_results:
        if "error" in r:
            lines.append(f"- google_flights {r['destination']}: ERREUR — {r['error']}")
        else:
            lines.append(
                f"- google_flights {r['destination']}: {r['itineraries_count']} itineraires, "
                f"JSON: `{r.get('raw_path', 'N/A')}`"
            )

    NOTES_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "=" * 60)
    print(f"Requetes consommees ce run : {total_requests}")
    print(f"Points de donnees exploitables (cumule) : {total_exploitable}")
    print(f"Conclusion suggeree : {suggested}")
    print(f"Rapport ecrit dans : {NOTES_PATH}")
    print("=" * 60)
    print("\n>>> Relis spike/SPIKE_NOTES.md, inspecte spike/responses/*.json, et confirme")
    print(">>> MANUELLEMENT le GO/NO-GO avant de commencer M3 (couche DB) et la suite.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
