"""Regenere webui/regions.json : noms francais -> identifiant Google (kgmid, "/m/...")
des pays et de quelques grandes regions. Source : Wikidata, propriete P646 (identifiant
Freebase, le meme espace d'identifiants que les kgmid de Google). Aucun appel SerpApi.

Wikidata refuse les scripts qui ne donnent pas de contact dans leur User-Agent : la requete
se lance donc a la main dans https://query.wikidata.org (QUERY ci-dessous), dont on exporte
les lignes au format "type|QID|libelle|kgmid" dans un fichier texte, puis :

    python -m webui.build_regions export.txt

L'interface n'utilise que regions.json (commite) et ne fait jamais d'appel reseau.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OUTPUT = Path(__file__).parent / "regions.json"

# Grandes regions utiles a une veille "exploration". Asie du Sud-Est (Q11708) est la seule
# verifiee par un vrai appel SerpApi (voir config/config.yaml).
REGION_QIDS = [
    "Q11708", "Q46", "Q48", "Q15", "Q49", "Q18", "Q27611", "Q664609", "Q55643", "Q7204",
    "Q27449", "Q27496", "Q27468", "Q27479", "Q21195", "Q27231", "Q771405", "Q27275",
    "Q27381", "Q4412", "Q27407", "Q27394",
]

# "r" = region (liste ci-dessus), "p" = etat souverain actuel (Q3624078 sans date de fin P576).
QUERY = """
SELECT ?kind ?item ?label ?fb WHERE {
  { VALUES ?item { %s } BIND("r" AS ?kind) }
  UNION
  { ?item wdt:P31 wd:Q3624078 . FILTER NOT EXISTS { ?item wdt:P576 ?end } BIND("p" AS ?kind) }
  ?item wdt:P646 ?fb .
  ?item rdfs:label ?label FILTER(LANG(?label) = "fr")
}
""" % " ".join(f"wd:{q}" for q in REGION_QIDS)

# Wikidata classe certains pays sous leur entite "royaume" (qui englobe des territoires
# d'outre-mer) : on les remplace par le pays lui-meme, celui qu'on cherche pour un voyage.
REPLACEMENTS = {
    "Q29999": ("Q55", "Pays-Bas", "/m/059j2"),
    "Q756617": ("Q35", "Danemark", "/m/0k6nt"),
}
RENAMES = {"Q148": "Chine"}


def build(rows: list[str]) -> list[dict]:
    by_item: dict[str, dict] = {}
    for line in rows:
        if not line.strip():
            continue
        kind, qid, label, fb = line.strip().split("|")
        if qid in REPLACEMENTS:
            qid, label, fb = REPLACEMENTS[qid]
        # Apostrophe droite : c'est celle qu'on tape au clavier dans la liste de l'interface.
        label = RENAMES.get(qid, label).replace("’", "'")
        entry = {"name": label[0].upper() + label[1:], "id": fb, "kind": "région" if kind == "r" else "pays", "wikidata": qid}
        current = by_item.get(qid)
        # Plusieurs P646 possibles pour un meme element : on garde l'identifiant principal (le plus court).
        if current is None or len(fb) < len(current["id"]):
            by_item[qid] = entry
    return sorted(by_item.values(), key=lambda e: (e["kind"] != "région", e["name"].casefold()))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        print(QUERY)
        sys.exit(1)
    data = build(Path(sys.argv[1]).read_text(encoding="utf-8").splitlines())
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(data)} entrees ecrites dans {OUTPUT}")
