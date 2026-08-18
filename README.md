# Flight Deal Aggregator OSL

Agrégateur personnel de bons plans de vols au départ d'Oslo (OSL). Ce n'est **pas** un
comparateur de recherche : le système observe les prix quotidiennement, les compare à leur
historique récent, et pousse une notification Telegram uniquement quand un vol est
anormalement bon marché par rapport à ses propres observations passées.

Voir le plan complet dans `C:\Users\admin\.claude\plans\parsed-hatching-oasis.md` pour
l'architecture détaillée, le schéma SQLite, la formule de score et la séquence de jalons.

## Principe

```
Scheduler (02:00 UTC, interne au conteneur)
  -> Collecte SerpApi (google_travel_explore, departure_id=OSL)
  -> Parsing / normalisation
  -> Stockage SQLite (historique append-only)
  -> Calcul des deals (médiane / percentile 25 / discount)
  -> Dédoublonnage
  -> Notification Telegram
```

## Statut actuel

Projet en cours de construction, jalon par jalon (voir tâches). **Aucun code applicatif
(DB / collecteur / analyse) ne sera écrit avant qu'un Spike technique jetable
(`spike/serpapi_spike.py`) ait validé que l'API SerpApi retourne des données OSL
exploitables** — voir `spike/SPIKE_NOTES.md` une fois généré.

## Setup local (avant de pouvoir lancer quoi que ce soit)

1. **Compte SerpApi** : créer un compte gratuit sur https://serpapi.com/manage-api-key et
   copier la clé.
2. **Bot Telegram** : parler à [@BotFather](https://t.me/BotFather) sur Telegram, créer un
   bot (`/newbot`), récupérer le token. Envoyer un message au bot, puis appeler
   `https://api.telegram.org/bot<TOKEN>/getUpdates` dans un navigateur pour récupérer son
   `chat_id` (`result[0].message.chat.id`).
3. Copier `.env.example` vers `.env` et renseigner `SERPAPI_KEY`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`. Ce fichier n'est jamais commité (voir `.gitignore`).
4. Installer les dépendances :
   ```bash
   pip install -r requirements-dev.txt
   ```

## Lancer le Spike

```bash
python spike/serpapi_spike.py
```

Consomme quelques requêtes SerpApi, sauvegarde les réponses JSON brutes dans
`spike/responses/`, et écrit un rapport `spike/SPIKE_NOTES.md` avec une conclusion
GO/NO-GO suggérée. À relire et confirmer manuellement avant de continuer le développement.

## Lancer les tests (une fois les modules applicatifs présents)

```bash
pytest
```

## Docker (à partir du jalon M10)

```bash
docker compose up -d
docker compose logs -f
```

La base SQLite et les logs sont persistés dans `./data` (bind mount), le fichier
`./config/config.yaml` peut être édité et pris en compte au prochain cycle sans rebuild.
