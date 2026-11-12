"""Regenere webui/airports.json : aeroports a vols reguliers ayant un code IATA, pour
l'autocompletion des champs Depart/Arrivee de l'interface. Aucun appel SerpApi.

Sources (a telecharger a la main, domaine public / CC0) :
- airports.csv : https://davidmegginson.github.io/ourairports-data/airports.csv (OurAirports)
- countries_fr.txt : lignes "ISO2|Nom du pays en francais" (Wikidata, P297 + libelle fr)

    python -m webui.build_airports airports.csv countries_fr.txt

L'interface n'utilise que airports.json (commite) et ne fait jamais d'appel reseau.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

OUTPUT = Path(__file__).parent / "airports.json"
TYPE_RANK = {"large_airport": 0, "medium_airport": 1, "small_airport": 2}


def build(airports_csv: Path, countries_txt: Path) -> list[list[str]]:
    countries = {}
    for line in countries_txt.read_text(encoding="utf-8").splitlines():
        if "|" in line:
            iso, name = line.split("|", 1)
            countries[iso.strip()] = name.strip()

    best: dict[str, tuple[int, list[str]]] = {}
    with airports_csv.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            code = (row.get("iata_code") or "").strip()
            rank = TYPE_RANK.get(row.get("type", ""))
            if rank is None or row.get("scheduled_service") != "yes" or not re.fullmatch(r"[A-Z]{3}", code):
                continue
            country = countries.get(row["iso_country"], row["iso_country"])
            entry = [code, (row.get("municipality") or "").strip(), row["name"].strip(), country]
            if code not in best or rank < best[code][0]:
                best[code] = (rank, entry)
    # Grands aeroports d'abord : l'autocompletion du navigateur propose les premiers en tete.
    return [e for _, e in sorted(best.values(), key=lambda item: (item[0], item[1][0]))]


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    data = build(Path(sys.argv[1]), Path(sys.argv[2]))
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{len(data)} aeroports ecrits dans {OUTPUT}")
