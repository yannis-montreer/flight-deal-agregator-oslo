# Flight Deal Aggregator OSL

Agrégateur personnel de bons plans de vols au départ d'Oslo (OSL). Ce n'est **pas** un
comparateur de recherche : le système observe les prix quotidiennement, les compare à leur
historique récent, et pousse une notification Telegram uniquement quand un vol est
anormalement bon marché par rapport à ses propres observations passées.

- **Spécifications d'origine** : [docs/SPEC.md](docs/SPEC.md) (texte intégral, archivé)
- **Plan d'implémentation approuvé** (architecture, schéma SQLite, formule de score, jalons) :
  `C:\Users\admin\.claude\plans\parsed-hatching-oasis.md`

## Statut actuel

MVP complet et déployé (jalons M0-M11 terminés). Tourne en continu via Docker sur cette
machine. Deux services distincts, une seule image :

| Service | Rôle | Périmètre actuel |
|---|---|---|
| `flightdeals` | Détection proactive de deals, produit principal | OSL → Asie du Sud-Est, novembre, séjours 6-14 nuits |
| `trip-watch-santiago` | Suivi ponctuel d'un trajet précis (favor) | OSL → Santiago, 13-17 janvier 2027, séjour 30-40j, 1 escale exacte, Premium Economy |

## Principe (flightdeals)

```
Scheduler (10:30 heure d'Oslo, CET/CEST géré automatiquement, interne au conteneur)
  -> Verification budget (compteur local + account.json SerpApi)
  -> Collecte SerpApi (google_travel_explore, departure_id=OSL, Asie du Sud-Est, novembre)
  -> Parsing / normalisation
  -> Stockage SQLite (historique append-only, flightdeals.db)
  -> Calcul des deals (médiane / percentile 25 / discount, filtres durée de vol + durée de séjour)
  -> Dédoublonnage
  -> Notification Telegram (deal détecté + récap quotidien systématique)
```

`trip-watch-santiago` suit une logique différente (point-à-point `google_flights`, rotation
de durée de séjour sur plusieurs jours) — voir [trip_watch/tracker.py](trip_watch/tracker.py).

`scripts/search_custom_route.py` reste disponible pour des recherches ponctuelles
(Lima, La Paz, ou toute autre combinaison route/dates) sans automatisation.

## Setup local

1. **Compte SerpApi** : créer un compte gratuit sur https://serpapi.com/manage-api-key et
   copier la clé.
2. **Bot Telegram** : parler à [@BotFather](https://t.me/BotFather) sur Telegram, créer un
   bot (`/newbot`), récupérer le token. Envoyer un message au bot, puis appeler
   `https://api.telegram.org/bot<TOKEN>/getUpdates` dans un navigateur pour récupérer son
   `chat_id` (`result[0].message.chat.id`).
3. Copier `.env.example` vers `.env` et renseigner `SERPAPI_KEY`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`. Optionnel : `SEARCH_SERPAPI_KEY` (clé séparée pour `trip_watch` et
   `scripts/search_custom_route.py`, recommandé pour ne pas consommer le quota principal).
   Ce fichier n'est jamais commité (voir `.gitignore`).
4. Installer les dépendances :
   ```bash
   pip install -r requirements-dev.txt
   ```

## Lancer les tests

```bash
pytest
```

185+ tests (voir `tests/`), aucun appel réseau réel (SerpApi et Telegram mockés partout sauf
dans `spike/` et `scripts/`, volontairement autonomes).

## Docker

```bash
docker compose up -d              # les 2 services
docker compose up -d flightdeals  # un seul
docker compose logs -f flightdeals
```

La base SQLite et les logs sont persistés dans `./data` (bind mount) :
`flightdeals.db` + `logs/` pour le produit principal, `trip_watch.db` + `logs-trip-watch/`
pour le suivi Santiago — totalement séparés, aucune interférence entre les deux.
`./config/config.yaml` (et `./trip_watch/config.yaml`) peuvent être édités et sont pris en
compte au prochain cycle sans rebuild — seul un changement de code nécessite
`docker compose build`.

## Spike (historique)

`spike/serpapi_spike.py` a validé le GO initial (voir `spike/SPIKE_NOTES.md`) avant que le
reste du système ne soit construit. Conservé pour référence, plus utilisé en fonctionnement
normal.
