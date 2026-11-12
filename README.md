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
machine. Trois services, deux images :

| Service | Rôle | Périmètre actuel |
|---|---|---|
| `flightdeals` | Détection proactive de deals, produit principal | OSL → Asie du Sud-Est, novembre, séjours 6-14 nuits |
| `trip-watch` | Alertes sur des trajets précis, une par fichier de `watches/` | ex. OSL → Santiago, 12-18 janvier 2027, séjour 30-40j, 1 escale exacte, Premium Economy |
| `webui` | Interface locale d'édition (http://127.0.0.1:8765) | voir plus bas |

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
  -> Signal Google en cold-start (voir ci-dessous)
```

**Signal Google (cold-start)** : tant qu'un bucket (destination/mois/durée) n'a pas les
`deal.minimum_observations` requises, notre propre détection statistique reste aveugle — un
vrai bon plan pourrait passer inaperçu les premiers jours. Palliatif : pour les quelques
candidats les moins chers du jour parmi ceux sans historique suffisant, une requête
`google_flights` supplémentaire demande à Google lui-même son avis (`price_insights.
price_level`). Si Google qualifie le prix de "low", un message Telegram **distinct** est
envoyé (jamais confondu avec un vrai deal statistique, dédoublonnage totalement séparé).
S'éteint tout seul dès qu'un bucket atteint le seuil — voir `cold_start_check` dans
`config/config.yaml` et `pipeline._check_cold_start_signals`.

`trip-watch` suit une logique différente (point-à-point `google_flights`, rotation de durée de
séjour sur plusieurs jours) — voir [trip_watch/tracker.py](trip_watch/tracker.py). Un seul service
fait tourner toutes les alertes de `watches/` (une par fichier `.yaml`), chacune avec sa clé
SerpApi (`api_key_env`), son heure et sa base (`db_file`) — voir
[trip_watch/runner.py](trip_watch/runner.py).

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

200+ tests (voir `tests/`), aucun appel réseau réel (SerpApi et Telegram mockés partout sauf
dans `spike/` et `scripts/`, volontairement autonomes).

## Docker

```bash
docker compose up -d              # les 3 services
docker compose up -d flightdeals  # un seul
docker compose logs -f flightdeals
```

La base SQLite et les logs sont persistés dans `./data` (bind mount) :
`flightdeals.db` + `logs/` pour le produit principal ; `logs-trip-watch/` et une base par alerte
(`trip_watch.db` pour Santiago, `trip_watch_<nom>.db` pour les autres) — totalement séparés.
`./config/config.yaml` (et `./watches/santiago.yaml`) peuvent être édités et sont pris en
compte au prochain cycle sans rebuild — seul un changement de code nécessite
`docker compose build`.

## Interface d'édition des veilles

Le service `webui` sert une page locale sur <http://127.0.0.1:8765> (depuis la machine
uniquement, par exemple via le Bureau à distance) pour modifier les veilles sans éditer le
YAML à la main :

- chaque enregistrement est validé par le même code que les services, puis écrit de façon
  atomique ; l'ancienne version est gardée dans `config/.history/` ou `watches/.history/`
  (30 dernières) ;
- le coût SerpApi mensuel (pire cas) est calculé par clé, et un enregistrement qui
  dépasserait le quota d'une clé est refusé (quota 250 par défaut, surchargeable via
  `WEBUI_KEY_QUOTAS` dans `docker-compose.yml`) ;
- l'interface n'a accès à aucun secret ni à `data/`.

**Alertes** : bouton « Nouvelle alerte » (fichier `watches/<nom>.yaml` créé, pris en compte en
quelques minutes sans redémarrage), case « Active » pour une pause, « Supprimer » range le
fichier dans `watches/.archive/` (l'historique `data/trip_watch_<nom>.db` est conservé).

**Clés** : chaque alerte choisit sa clé par **nom** de variable de `.env` (`api_key_env`). Le
service `trip-watch` publie dans `status/keys.json` les noms des clés présentes et les compteurs
de leur compte (`account.json`, gratuit) — jamais les valeurs. Ajouter une clé : une ligne
`SERPAPI_KEY_3=...` dans `.env` (le nom doit contenir `SERPAPI_KEY`), puis `docker compose up -d`.

## Spike (historique)

`spike/serpapi_spike.py` a validé le GO initial (voir `spike/SPIKE_NOTES.md`) avant que le
reste du système ne soit construit. Conservé pour référence, plus utilisé en fonctionnement
normal.
