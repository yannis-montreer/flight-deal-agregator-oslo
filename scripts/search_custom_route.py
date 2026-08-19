"""
Recherche ad-hoc point-a-point sur une plage de dates de depart et de duree de sejour, pour
plusieurs destinations candidates. Script DISTINCT et INDEPENDANT du produit principal
(flightdeals/) : n'ecrit rien dans flightdeals.db, ne partage aucun etat avec lui, ne tourne
pas dans Docker, pas de scheduler — un run manuel = une recherche.

Pourquoi un script separe : le spec du produit exclut explicitement la "recherche de vols a
la demande" du MVP (section 2.2) — le systeme principal DETECTE des deals de maniere
proactive, il ne REPOND pas a une demande ponctuelle "trouve-moi le meilleur prix pour CE
voyage precis". C'est exactement ce que fait ce script, avec l'autre mode de l'API SerpApi
deja valide au Spike mais jamais utilise en prod : `google_flights` (point-a-point, dates
precises) plutot que `google_travel_explore` (scan large, dates approximatives).

Cle API : --api-key, sinon SEARCH_SERPAPI_KEY, sinon SERPAPI_KEY (celle du systeme principal,
consomme SON quota) en dernier recours. Utiliser une cle separee est fortement recommande
pour ce genre de recherche ponctuelle (voir le README du repo).

COUT : nombre de requetes = nb_destinations x nb_dates_depart x nb_durees testees. Avec une
tolerance large ca grimpe vite — --duration-step-days et --departure-step-days controlent
la densite de la grille. Le script affiche le nombre EXACT de requetes AVANT le moindre
appel reseau, refuse de continuer au-dela de --max-requests, et demande une confirmation
interactive (sauf --yes).

Exemple (le besoin qui a motive ce script : OSL -> Santiago/Lima/La Paz, depart 15 janvier
2027 +/- 2j, sejour 5 semaines +/- 5j) :

    python scripts/search_custom_route.py \\
        --destinations SCL LIM LPB \\
        --departure 2027-01-15 --departure-tolerance-days 2 \\
        --duration-days 35 --duration-tolerance-days 5 --duration-step-days 5
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import httpx

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

SERPAPI_BASE_URL = "https://serpapi.com/search.json"
RESULTS_DIR = Path(__file__).parent / "search_results"

# Complete/corrige cette table si tu ajoutes d'autres destinations frequentes.
DESTINATION_NAMES = {
    "SCL": "Santiago",
    "LIM": "Lima",
    "LPB": "La Paz",
}


@dataclass(frozen=True)
class SearchResult:
    destination: str
    destination_name: str
    departure_date: str
    return_date: str
    price: Optional[float]
    currency: str
    airline: Optional[str]
    stops: Optional[int]
    duration_minutes: Optional[int]
    booking_url: Optional[str]
    error: Optional[str] = None


def _api_key(cli_key: Optional[str]) -> str:
    key = cli_key or os.environ.get("SEARCH_SERPAPI_KEY") or os.environ.get("SERPAPI_KEY", "")
    key = key.strip()
    if not key:
        print(
            "ERREUR: aucune cle API. Passe --api-key, ou definis SEARCH_SERPAPI_KEY "
            "(recommande, cle separee) ou SERPAPI_KEY (cle du systeme principal, "
            "consomme SON quota) dans .env.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def _offsets(tolerance_days: int, step_days: int) -> list[int]:
    """Liste d'offsets en jours de -tolerance a +tolerance, au pas donne, en garantissant
    que la borne haute exacte est toujours testee meme si le pas ne la touche pas pile."""
    if tolerance_days == 0:
        return [0]
    offsets = list(range(-tolerance_days, tolerance_days + 1, step_days))
    if offsets[-1] != tolerance_days:
        offsets.append(tolerance_days)
    return sorted(set(offsets))


def build_grid(
    *,
    departure_center: date,
    departure_tolerance_days: int,
    departure_step_days: int,
    duration_center_days: int,
    duration_tolerance_days: int,
    duration_step_days: int,
) -> list[tuple[date, date]]:
    """Toutes les paires (date_depart, date_retour) a tester, croisement des deux plages."""
    departure_dates = [departure_center + timedelta(days=o) for o in _offsets(departure_tolerance_days, departure_step_days)]
    durations = [duration_center_days + o for o in _offsets(duration_tolerance_days, duration_step_days)]

    return [(dep, dep + timedelta(days=dur)) for dep in departure_dates for dur in durations]


def search_one(
    client: httpx.Client, api_key: str, *, origin: str, destination: str,
    outbound_date: date, return_date: date, currency: str,
) -> SearchResult:
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": outbound_date.isoformat(),
        "return_date": return_date.isoformat(),
        "type": "1",  # round trip selon la doc SerpApi au moment de l'ecriture
        "currency": currency,
        "api_key": api_key,
    }

    def _error_result(message: str) -> SearchResult:
        return SearchResult(
            destination=destination,
            destination_name=DESTINATION_NAMES.get(destination, destination),
            departure_date=outbound_date.isoformat(),
            return_date=return_date.isoformat(),
            price=None, currency=currency, airline=None, stops=None, duration_minutes=None,
            booking_url=None, error=message,
        )

    try:
        response = client.get(SERPAPI_BASE_URL, params=params, timeout=30.0)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _error_result(f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except httpx.HTTPError as exc:
        return _error_result(f"reseau: {exc}")

    data = response.json()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RESULTS_DIR / f"{destination}_{outbound_date.isoformat()}_{return_date.isoformat()}.json"
    raw_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    candidates = data.get("best_flights") or data.get("other_flights") or []
    if not candidates:
        return _error_result(data.get("error") or "aucun vol trouve")

    best = candidates[0]
    legs = best.get("flights", [])
    airlines = ", ".join(sorted({leg.get("airline", "?") for leg in legs})) if legs else None

    return SearchResult(
        destination=destination,
        destination_name=DESTINATION_NAMES.get(destination, destination),
        departure_date=outbound_date.isoformat(),
        return_date=return_date.isoformat(),
        price=best.get("price"),
        currency=currency,
        airline=airlines,
        stops=max(len(legs) - 1, 0) if legs else None,
        duration_minutes=best.get("total_duration"),
        booking_url=data.get("search_metadata", {}).get("google_flights_url"),
    )


def _write_csv(results: list[SearchResult]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = RESULTS_DIR / "resultats.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "destination", "destination_name", "departure_date", "return_date", "duree_jours",
            "price", "currency", "airline", "stops", "duration_minutes", "booking_url", "error",
        ])
        for r in results:
            duree = (date.fromisoformat(r.return_date) - date.fromisoformat(r.departure_date)).days
            writer.writerow([
                r.destination, r.destination_name, r.departure_date, r.return_date, duree,
                r.price, r.currency, r.airline, r.stops, r.duration_minutes, r.booking_url, r.error,
            ])
    return csv_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--origin", default="OSL")
    parser.add_argument("--destinations", nargs="+", required=True, help="Codes IATA, ex: SCL LIM LPB")
    parser.add_argument("--departure", required=True, help="Date de depart centrale, YYYY-MM-DD")
    parser.add_argument("--departure-tolerance-days", type=int, default=2)
    parser.add_argument("--departure-step-days", type=int, default=1)
    parser.add_argument("--duration-days", type=int, required=True, help="Duree de sejour centrale, en jours")
    parser.add_argument("--duration-tolerance-days", type=int, default=5)
    parser.add_argument(
        "--duration-step-days", type=int, default=5,
        help="Pas d'echantillonnage de la duree (defaut 5j - reduit le cout ; 1 = teste chaque jour)",
    )
    parser.add_argument("--currency", default="NOK")
    parser.add_argument("--api-key", default=None, help="Sinon SEARCH_SERPAPI_KEY puis SERPAPI_KEY depuis .env")
    parser.add_argument(
        "--max-requests", type=int, default=60,
        help="Garde-fou : refuse de lancer si le plan depasse ce nombre de requetes (defaut 60)",
    )
    parser.add_argument("--yes", action="store_true", help="Ne pas demander confirmation avant de lancer")
    args = parser.parse_args()

    departure_center = date.fromisoformat(args.departure)
    grid = build_grid(
        departure_center=departure_center,
        departure_tolerance_days=args.departure_tolerance_days,
        departure_step_days=args.departure_step_days,
        duration_center_days=args.duration_days,
        duration_tolerance_days=args.duration_tolerance_days,
        duration_step_days=args.duration_step_days,
    )
    total_requests = len(grid) * len(args.destinations)

    print(f"Destinations: {', '.join(args.destinations)}")
    print(f"Dates de depart testees: {sorted({d.isoformat() for d, _ in grid})}")
    print(f"Durees de sejour testees (jours): {sorted({(r - d).days for d, r in grid})}")
    print(f"=> {len(grid)} combinaisons date/duree x {len(args.destinations)} destinations = {total_requests} requetes SerpApi")

    if total_requests > args.max_requests:
        print(
            f"\nARRET: {total_requests} requetes depasse --max-requests={args.max_requests}. "
            f"Augmente --max-requests si c'est voulu, ou reduis la couverture "
            f"(--duration-step-days, --departure-step-days, moins de destinations).",
            file=sys.stderr,
        )
        return 1

    if not args.yes:
        confirm = input(f"\nLancer {total_requests} requetes maintenant ? [y/N] ").strip().lower()
        if confirm != "y":
            print("Annule.")
            return 0

    api_key = _api_key(args.api_key)
    results: list[SearchResult] = []
    done = 0
    with httpx.Client() as client:
        for destination in args.destinations:
            for outbound_date, return_date in grid:
                done += 1
                result = search_one(
                    client, api_key, origin=args.origin, destination=destination,
                    outbound_date=outbound_date, return_date=return_date, currency=args.currency,
                )
                results.append(result)
                price_label = f"{result.price} {result.currency}" if result.price is not None else f"ERREUR: {result.error}"
                print(f"  [{done}/{total_requests}] {destination} {outbound_date} -> {return_date}: {price_label}")

    valid_results = sorted((r for r in results if r.price is not None), key=lambda r: r.price)

    print("\n=== Meilleurs prix trouves ===")
    if not valid_results:
        print("  Aucun resultat exploitable — verifie les fichiers JSON dans scripts/search_results/ pour comprendre pourquoi.")
    for r in valid_results[:15]:
        duree = (date.fromisoformat(r.return_date) - date.fromisoformat(r.departure_date)).days
        print(
            f"  {r.destination_name} ({r.destination})  {r.departure_date} -> {r.return_date} "
            f"({duree}j)  :  {r.price} {r.currency}  |  {r.airline}  |  {r.stops} escale(s)"
        )

    csv_path = _write_csv(results)
    print(f"\nTous les resultats (y compris erreurs): {csv_path}")
    print(f"JSON bruts par requete: {RESULTS_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
