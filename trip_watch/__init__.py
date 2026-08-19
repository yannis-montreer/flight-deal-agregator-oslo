"""trip_watch — suivi quotidien de prix pour un trajet specifique (OSL -> Santiago,
janvier 2027), distinct du produit principal flightdeals/.

Contexte : demande ponctuelle d'une amie (voir conversation), hors perimetre du MVP
flightdeals (qui exclut explicitement la recherche a la demande). Reutilise les briques
du produit principal (SerpApiClient, envoi Telegram, seconds_until_next_run) mais avec sa
propre logique de collecte (google_flights point-a-point plutot que google_travel_explore)
et sa propre base SQLite, totalement independante de flightdeals.db.
"""
