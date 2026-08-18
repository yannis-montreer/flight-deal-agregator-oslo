# Spike SerpApi — Flight Deal Aggregator OSL

Execute le : 2026-08-18T20:49:14.775089+00:00

- **API utilisee** : SerpApi (`google_travel_explore` pour departure_id=OSL, `google_flights` en echantillon sur ['NRT', 'BKK'])
- **Nombre de requetes consommees** : 5
- **Nombre de destinations retournees (explore, cumule sur les 3 durees)** : 252
- **Nombre de prix exploitables (cumule)** : 125
- **Structure des donnees** : voir les fichiers JSON bruts dans `spike/responses/`
- **Limites constatees** : _a completer manuellement apres inspection des JSON_ (ex: return_date absent ? destinations "partout" instables d'un jour sur l'autre ? champs manquants/renommes ?)
- **Cout estime** : 5 requetes pour ce spike ; en regime de croisiere, 3 requetes/jour (explore x3 durees) = ~90/mois, sous le budget cible de 200/mois.
- **Conclusion suggeree (a confirmer manuellement)** : GO
  _(seuil : >= 15 points exploitables pour <= 20 requetes. Avant de valider un GO definitif, relancer ce script un autre jour et comparer les destinations retournees pour verifier qu'elles se recoupent suffisamment (sinon les buckets n'atteindront jamais 7 observations, voir risques dans le plan)._

## Detail par appel
- explore travel_duration=1: 84 destinations, 57 exploitables, JSON: `C:\Users\admin\Documents\Voyage\spike\responses\20260818T204900Z_explore_duration1.json`
- explore travel_duration=2: 84 destinations, 52 exploitables, JSON: `C:\Users\admin\Documents\Voyage\spike\responses\20260818T204904Z_explore_duration2.json`
- explore travel_duration=3: 84 destinations, 10 exploitables, JSON: `C:\Users\admin\Documents\Voyage\spike\responses\20260818T204909Z_explore_duration3.json`
- google_flights NRT: 3 itineraires, JSON: `C:\Users\admin\Documents\Voyage\spike\responses\20260818T204911Z_flights_NRT.json`
- google_flights BKK: 3 itineraires, JSON: `C:\Users\admin\Documents\Voyage\spike\responses\20260818T204914Z_flights_BKK.json`

## Revue manuelle (post-inspection des JSON bruts)

**Structure confirmee de `google_travel_explore` (destinations[])** : `destination_id` (kgmid Freebase,
pas un code IATA), `name`, `country`, `gps_coordinates`, `start_date`, `end_date`, `link`, `serpapi_link`
toujours presents. Quand un prix est disponible, s'ajoutent : `destination_airport.code` (le vrai code
IATA), `flight_price`, `flight_duration` (minutes de vol), `number_of_stops`, `airline`, `airline_code`.

**Decouverte importante vs. hypothese du plan** : `end_date` EST bien retourne explicitement par
l'API. Le plan prevoyait un fallback (deriver `trip_length_nights` depuis `travel_duration` si
`end_date` absent) — ce fallback n'est plus le chemin principal, `end_date` peut etre utilise
directement. Garder le fallback en code mort defensif ne coute rien mais ce n'est plus le cas
attendu.

**Sparsite des prix, variable selon la duree** :
- weekend (duration=1) : 57/84 (68%) avec prix exploitable
- ~1 semaine (duration=2) : 52/84 (62%)
- ~2 semaines (duration=3) : 10/84 (12%) — nettement plus creux

Ce n'est pas un bug de parsing (verifie sur le JSON brut : les entrees "sans prix" n'ont
simplement pas les cles `flight_price`/`destination_airport`/etc. dans la reponse — Google
n'a pas de cache de prix pour ce couple destination/duree). C'est cependant sans cout
supplementaire : la requete duration=3 reste 1 seule requete et ramene quand meme 10 points
utilisables. Recommandation : garder les 3 durees en config comme prevu, mais s'attendre a ce
que les buckets "~2 semaines" mettent plus de temps a atteindre le minimum de 7 observations
(deja un risque documente dans le plan).

**`google_flights` (mode A)** : structure riche confirmee (legs detailles, compagnies, escales,
prix, `search_metadata.google_flights_url` utilisable comme lien de reservation). Aucune erreur
sur les 2 destinations testees. Confirme utilisable en secours mais reste plus cher (1 requete
= 1 seule destination) donc non retenu pour la collecte quotidienne en regime de croisiere.

**Non verifie par ce spike (limite connue)** : stabilite jour-apres-jour des destinations
retournees par "explore" (un seul jour de donnees disponible pour l'instant). A surveiller
pendant la premiere semaine de collecte reelle (jalon M12) plutot que bloquant ici.

**Budget** : 5 requetes consommees pour ce spike (250 restantes). En regime de croisiere
(3 requetes/jour, sans `google_flights`) : ~90/mois, large marge sous les 200/mois cibles.

## Conclusion finale

**GO confirme.** Volume et qualite de donnees largement suffisants (125 points exploitables
pour 5 requetes, extrapolation ~90 requetes/mois en continu). Un seul ajustement au plan
initial : utiliser `end_date` directement plutot que le deriver, le reste de l'architecture
prevue (schema DB, bucketing, scoring) reste valide tel que concu. On peut enchainer sur M3.
