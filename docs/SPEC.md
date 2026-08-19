# Spécifications MVP — Flight Deal Aggregator OSL

> Document original fourni par l'utilisateur au lancement du projet (2026-08-19), reproduit
> ici tel quel pour archivage — voir [../README.md](../README.md) pour l'état actuel du
> projet et le plan d'implémentation approuvé (`C:\Users\admin\.claude\plans\parsed-hatching-oasis.md`)
> pour l'architecture technique dérivée de ce spec. Les décisions prises en cours de route
> qui s'écartent de ce document (recentrage Asie du Sud-Est/novembre, filtre durée de
> séjour, horaire Europe/Oslo, etc.) sont documentées dans l'historique git et les
> commentaires du code, pas retro-éditées ici.

## 1. Spécifications MVP — Flight Deal Aggregator OSL

### 1.1 Objectif produit

Construire un agrégateur personnel de bons plans de vols au départ d'Oslo (OSL).
Le produit doit identifier automatiquement les vols dont le prix est anormalement bas par rapport à leur historique récent, puis notifier l'utilisateur lorsqu'un deal suffisamment intéressant est détecté.

Le produit n'est pas un comparateur de vols classique. Il ne cherche pas à permettre à l'utilisateur de rechercher manuellement un vol. Son objectif est la détection proactive d'opportunités.

**Exemple**

Le système détecte :
```
OSL → Tokyo
3 200 NOK A/R
-42 % par rapport au prix historique
12–19 janvier
1 escale
```
Il envoie alors une notification Telegram.

## 2. Périmètre MVP

### 2.1 Inclus

Le MVP doit :

- utiliser OSL comme unique aéroport de départ ;
- rechercher des destinations internationales ;
- récupérer quotidiennement les prix disponibles via une API de vols ;
- conserver l'historique des prix ;
- conserver au minimum 30 jours d'historique ;
- comparer le prix courant aux prix historiques ;
- calculer un score de deal ;
- identifier les deals dépassant un seuil ;
- éviter de notifier plusieurs fois le même deal ;
- envoyer une notification Telegram ;
- fonctionner automatiquement une fois déployé ;
- respecter un budget API minimal.

### 2.2 Exclus du MVP

Ne pas développer :

- réservation de billets ;
- paiement ;
- comptes utilisateurs ;
- application mobile ;
- interface web complexe ;
- scraping de compagnies aériennes ;
- gestion de plusieurs utilisateurs ;
- recommandations personnalisées ;
- programme de fidélité/miles ;
- prédiction ML ;
- recherche de vols à la demande.

## 3. Source de données

**API cible**

Le MVP utilisera SerpApi / Google Flights.

Deux types de recherche doivent être étudiés :

**A. Google Flights Deals**

Objectif : récupérer les deals déjà identifiés par Google Flights.

Paramètre principal :
```
departure_id=OSL
```

**B. Google Travel Explore**

Objectif : explorer les destinations disponibles au départ d'OSL.

Les recherches doivent couvrir au minimum :

- court séjour / weekend ;
- environ 1 semaine ;
- environ 2 semaines.

Le développeur devra mesurer le nombre réel de destinations retournées et vérifier la couverture effective.

## 4. Contrainte API

Le système doit être conçu autour d'un quota gratuit de l'ordre de 250 recherches/mois.

Le système doit donc fonctionner avec un budget cible de :
**≤ 200 requêtes API/mois**
afin de conserver une marge pour les tests et erreurs.

**Important**

Il est interdit de contourner artificiellement les limites du fournisseur par multiplication de comptes ou de clés API sans validation explicite des conditions du fournisseur.

## 5. Fréquence de collecte

Le MVP effectue une collecte :
**1 fois par jour**

La collecte doit être automatisée par un scheduler.

Exemple :
```
02:00 UTC
   ↓
Collecte API
   ↓
Parsing
   ↓
Stockage SQLite
   ↓
Calcul des deals
   ↓
Notifications Telegram
```

L'heure exacte doit être configurable.

## 6. Données à stocker

SQLite sera utilisé pour le MVP.

**Table `flight_observations`**
```
id
observed_at
origin
destination
destination_name
departure_date
return_date
price
currency
airline
stops
duration_minutes
source
source_url
```

Exemple :
```
2026-08-17
OSL
NRT
Tokyo
2027-01-12
2027-01-19
3200
NOK
SAS
1
...
```

## 7. Historique

Le système doit conserver les observations.

La durée minimale de conservation est :
**30 jours**

Mais aucune suppression automatique n'est nécessaire dans le MVP.

La base doit donc naturellement pouvoir accumuler plusieurs mois de données.

## 8. Normalisation des données

Les prix ne doivent pas être comparés aveuglément.

Une observation doit idéalement être comparée avec des vols ayant :

- même origine ;
- même destination/aéroport ;
- durée de voyage similaire ;
- période de voyage similaire ;
- nombre d'escales comparable lorsque disponible.

Exemple :
```
OSL → NRT
7 jours
janvier
```
doit être comparé prioritairement à :
```
OSL → NRT
5–9 jours
janvier
```
et non à un billet :
```
OSL → NRT
21 jours
juillet
```

## 9. Détection des deals

Le MVP ne nécessite pas de machine learning.
Utiliser une méthode statistique simple et robuste.

Pour chaque route :
```
historique = observations pertinentes des 30 derniers jours
```

Calculer notamment :

- médiane ;
- minimum ;
- percentile 25 ;
- nombre d'observations.

**Discount principal**
```
discount = 1 - current_price / historical_median
```

Exemple :
```
Prix actuel :       3 000 NOK
Médiane 30 jours :  5 000 NOK
Discount = 40 %
```

## 10. Règle MVP de déclenchement

Un deal peut être envoyé si :
```
nombre_observations >= 7
ET
discount >= 30 %
ET
prix actuel <= percentile 25
```

Pour éviter les faux positifs :
```
si historique < 7 observations
→ aucune notification
```

Le seuil doit être configurable sans modification du code.

Configuration exemple :
```yaml
deal:
  minimum_discount: 0.30
  minimum_observations: 7
  percentile_threshold: 25
```

## 11. Score de deal

Le système doit produire un score simple de 0 à 100.

Exemple conceptuel :
```
40 % moins cher que la médiane
+
prix dans les 10 % les moins chers
+
vol direct
+
historique suffisamment important
=
score élevé
```

Le détail exact du calcul peut être défini par l'équipe technique, mais il doit être déterministe, documenté et configurable.

Le MVP n'a pas besoin d'intelligence artificielle.

## 12. Gestion des doublons

Le même deal ne doit pas être envoyé quotidiennement.

Créer une clé logique :
```
origin
destination
departure_date
return_date
```

Si un deal déjà notifié reste présent le lendemain :
**aucune nouvelle notification.**

Une nouvelle notification peut être envoyée si :

- le prix baisse significativement ;
- les dates changent ;
- le deal disparaît puis réapparaît après une période définie.

## 13. Notification Telegram

Le MVP utilise un Telegram Bot.

Format souhaité :
```
🔥 FLIGHT DEAL
OSL → Tokyo (NRT)
3 200 NOK A/R
-40 % vs historique
12 → 19 janvier
SAS
1 escale
Deal score: 87/100
Voir le vol :
[Google Flights]
```

La notification doit contenir :

- origine ;
- destination ;
- prix ;
- devise ;
- dates ;
- compagnie si disponible ;
- escales ;
- discount historique ;
- score ;
- lien vers le résultat.

## 14. Gestion des erreurs

Le système doit gérer :

- API indisponible ;
- timeout ;
- quota dépassé ;
- réponse invalide ;
- destination sans prix ;
- données incomplètes ;
- Telegram indisponible.

Une erreur sur une destination ne doit pas arrêter toute la collecte.

Les erreurs doivent être enregistrées dans les logs.

## 15. Configuration

Aucune donnée sensible ne doit être hardcodée.

Exemple :
```
SERPAPI_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```
via variables d'environnement.

Configuration fonctionnelle dans un fichier :
```yaml
origin: OSL
currency: NOK
collection:
  enabled: true
  schedule: "02:00"
deal:
  minimum_discount: 0.30
  minimum_observations: 7
  percentile_threshold: 25
notification:
  telegram: true
```

## 16. Architecture technique MVP

```
                  ┌──────────────────┐
                  │ Scheduler        │
                  │ Cron              │
                  └────────┬─────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │ Flight API       │
                  │ SerpApi           │
                  └────────┬─────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │ Parser /         │
                  │ Normalizer       │
                  └────────┬─────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │ SQLite           │
                  │ Price History    │
                  └────────┬─────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │ Deal Detector    │
                  └────────┬─────────┘
                           │
                    deal detected
                           │
                           ▼
                  ┌──────────────────┐
                  │ Deduplication    │
                  └────────┬─────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │ Telegram Bot     │
                  └──────────────────┘
```

## 17. Stack technique

**Backend** : Python 3.12+

**API** : SerpApi

**Database** : SQLite. Pas besoin de PostgreSQL pour le MVP.

**HTTP** : `requests` ou `httpx`

**Data processing** : `statistics` / `numpy` si nécessaire. Éviter pandas dans le cœur du système si son utilisation n'apporte pas de bénéfice.

**Scheduler** : Pour le MVP :
- cron Linux ;
- ou GitHub Actions ;
- ou Docker + cron.

**Notifications** : Telegram Bot API.

**Déploiement** : Un petit VPS ou Raspberry Pi/ordinateur personnel suffit largement pour la charge prévue.

## 18. Critères d'acceptation MVP

Le MVP est considéré comme terminé lorsque :

**Collecte**
- Une exécution quotidienne peut être déclenchée automatiquement.
- OSL est utilisé comme origine.
- Les résultats API sont récupérés et parsés.
- Les erreurs API sont gérées.
- Les résultats sont enregistrés dans SQLite.

**Historique**
- Deux observations du même vol peuvent être comparées.
- L'historique de 30 jours est exploitable.
- Les observations sont correctement normalisées.

**Détection**
- La médiane historique est calculée.
- Le discount est calculé.
- Le minimum d'observations est respecté.
- Le seuil de deal est configurable.
- Les faux doublons sont évités.

**Notification**
- Un deal valide déclenche Telegram.
- Un même deal n'est pas envoyé quotidiennement.
- Le message contient les informations essentielles.
- Le lien vers le vol est fonctionnel.

**Exploitation**
- Le système peut fonctionner sans intervention humaine.
- Les clés API sont externalisées.
- Les logs permettent de diagnostiquer une erreur.
- Le nombre de requêtes mensuelles est mesurable.

## 19. Phase 2 — hors MVP

Une fois le MVP validé :

**Détection plus sophistiquée**
- saisonnalité ;
- comparaison par mois ;
- moyenne mobile ;
- percentile historique ;
- score de confiance ;
- détection de mistake fares.

**Interface web**

Dashboard :
```
🔥 Deals
├── Tokyo       3 200 NOK   -43 %
├── New York    2 100 NOK   -41 %
└── Bangkok     3 700 NOK   -52 %
```

Filtres :
- destination ;
- prix maximum ;
- discount minimum ;
- nombre d'escales ;
- durée ;
- continent.

**Personnalisation**

Plusieurs aéroports :
```
OSL
TRF
CPH
ARN
```
et préférences utilisateur.

## 20. Spécification produit complète — contexte pour un nouveau développeur

### Flight Deal Aggregator — OSL

**Contexte**

L'utilisateur vit en Norvège et souhaite découvrir des billets d'avion exceptionnellement bon marché au départ d'Oslo.

Les comparateurs classiques comme Google Flights, Skyscanner ou Kayak répondent à une demande de recherche : l'utilisateur doit savoir où et quand il souhaite voyager.

Les plateformes de type Secret Flying répondent davantage au besoin de découverte de deals, mais ne permettent pas toujours de filtrer suffisamment précisément le point de départ dans leur offre gratuite.

Le besoin est donc de créer un système automatisé qui inverse le fonctionnement :

> L'utilisateur ne cherche pas un vol. Le système cherche les opportunités pour lui.

**Problème à résoudre**

Un prix de 3 000 NOK n'est pas nécessairement un bon prix.

Exemple :
```
OSL → Tokyo
Prix actuel : 3 500 NOK
```
Ce prix n'a de sens que si l'on connaît le prix habituel.

Si les observations précédentes donnent :
```
4 900
5 200
5 400
4 800
5 100
5 600
```
alors 3 500 NOK est probablement intéressant.

Le produit doit donc construire son propre historique de prix et détecter les écarts.

**Vision**

À terme, le produit doit devenir :

> « Dealabs pour les vols au départ d'Oslo. »

L'utilisateur reçoit uniquement les opportunités réellement intéressantes, sans avoir à effectuer de recherche.

**Principe fondamental**

Le système fonctionne en quatre étapes :
```
1. OBSERVER
   ↓
2. STOCKER
   ↓
3. COMPARER
   ↓
4. ALERTER
```

- **Observer** : Récupérer quotidiennement les prix disponibles via une API.
- **Stocker** : Conserver les observations dans SQLite.
- **Comparer** : Comparer le prix actuel à son historique.
- **Alerter** : Notifier uniquement lorsqu'un seuil de deal est atteint.

**Contraintes principales**

- **Coût** : Le système doit initialement fonctionner avec un budget API nul ou quasi nul.
- **Volume** : Il faut couvrir autant de destinations que possible à partir d'OSL.
- **Fréquence** : Une collecte quotidienne suffit pour le MVP.
- **Simplicité** : Pas de machine learning ni d'architecture distribuée.
- **Fiabilité** : Une erreur API ponctuelle ne doit pas empêcher les jours suivants de fonctionner.

**Première tâche de l'équipe**

Avant de développer toute l'application, l'équipe doit réaliser un Spike technique.

**Spike**

Créer un petit script Python qui :

1. appelle Google Flights Deals pour `OSL` ;
2. appelle Google Travel Explore pour `OSL` avec les différentes durées ;
3. sauvegarde la réponse JSON brute ;
4. compte le nombre de destinations ;
5. affiche les destinations ;
6. affiche les prix ;
7. mesure le nombre de requêtes nécessaires ;
8. vérifie que les dates et prix sont suffisamment exploitables pour construire un historique.

**Livrable**

Un rapport très court :
```
API utilisée :
Nombre de requêtes :
Nombre de destinations retournées :
Nombre de prix exploitables :
Structure des données :
Limites constatées :
Coût estimé :
Conclusion : GO / NO-GO
```

Le développement du reste du MVP ne doit commencer qu'après ce Spike.

Cela évite de construire toute l'architecture SQLite + détection + Telegram autour d'une hypothèse incorrecte sur la couverture réelle de l'API.
