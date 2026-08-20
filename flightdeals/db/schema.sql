-- Schema SQLite du Flight Deal Aggregator OSL.
-- Toutes les instructions sont idempotentes (IF NOT EXISTS) : ce fichier est reexecute via
-- executescript() a chaque ouverture de connexion (voir connection.py). Pas de framework de
-- migration : le schema MVP est fixe, une evolution future se ferait via un nouveau fichier.

-- Chaque observation de prix jamais collectee. Append-only, croissance illimitee acceptee
-- (voir spec section 7 : pas de suppression automatique necessaire pour le MVP).
CREATE TABLE IF NOT EXISTS flight_observations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at          TEXT    NOT NULL,   -- ISO8601 UTC, capture au debut du run (anti-auto-comparaison)
    origin               TEXT    NOT NULL,
    destination          TEXT    NOT NULL,
    destination_name     TEXT,               -- nullable, affichage seulement
    departure_date       TEXT    NOT NULL,   -- YYYY-MM-DD
    return_date          TEXT,               -- YYYY-MM-DD, NULL si one-way
    price                REAL    NOT NULL,
    currency             TEXT    NOT NULL,
    airline              TEXT,               -- nullable, l'API l'omet parfois
    stops                INTEGER,            -- nullable ; 0 = direct
    duration_minutes     INTEGER,            -- duree de VOL (flight_duration), PAS la duree du sejour
    source                TEXT    NOT NULL,  -- 'serpapi_google_travel_explore' | 'serpapi_google_flights'
    source_url            TEXT,

    -- champs calcules a l'ecriture par flightdeals.analysis.bucketing (jalon M4)
    trip_length_nights    INTEGER,           -- NULL si one-way (pas de return_date)
    travel_period_bucket  TEXT    NOT NULL,  -- 'YYYY-MM' de departure_date
    stops_bucket          TEXT    NOT NULL   -- 'nonstop' | 'one_stop' | 'multi_stop' | 'unknown'
);

CREATE INDEX IF NOT EXISTS idx_obs_compare
    ON flight_observations (origin, destination, travel_period_bucket, trip_length_nights, observed_at);

CREATE INDEX IF NOT EXISTS idx_obs_dedup_lookup
    ON flight_observations (origin, destination, departure_date, return_date);

-- Log append-only des notifications reellement ENVOYEES (pas juste tentees) — pilote la dedup
-- (jalon M6). Une ligne n'est ecrite qu'apres un envoi Telegram confirme (200 + ok:true).
CREATE TABLE IF NOT EXISTS notified_deals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    notified_at      TEXT    NOT NULL,   -- ISO8601 UTC
    origin           TEXT    NOT NULL,
    destination      TEXT    NOT NULL,
    departure_date   TEXT    NOT NULL,
    return_date      TEXT,
    price            REAL    NOT NULL,   -- prix au moment de CETTE notification (base du calcul "baisse suffisante")
    currency         TEXT    NOT NULL,
    score            INTEGER NOT NULL,
    discount         REAL    NOT NULL,
    observation_id   INTEGER NOT NULL REFERENCES flight_observations(id)
);

CREATE INDEX IF NOT EXISTS idx_notified_key_time
    ON notified_deals (origin, destination, departure_date, return_date, notified_at);

-- Compteur local de budget API mensuel (ceinture-bretelles en plus du endpoint account.json
-- gratuit de SerpApi, qui reste la source primaire consultee en debut de run).
CREATE TABLE IF NOT EXISTS api_usage (
    month            TEXT PRIMARY KEY,   -- 'YYYY-MM'
    requests_used    INTEGER NOT NULL DEFAULT 0,
    last_updated_at  TEXT NOT NULL
);

-- Log append-only des "signaux Google" envoyes : cold-start uniquement (observation sans
-- assez d'historique pour notre propre detection statistique), quand price_insights.price_level
-- (engine google_flights) qualifie le prix de "low". Table VOLONTAIREMENT separee de
-- notified_deals — meme structure/usage (pilote should_notify_google_signal, meme logique
-- de dedup que les vrais deals) mais jamais melangee : un signal Google non confirme ne doit
-- jamais supprimer, ni etre supprime par, un vrai deal statistique sur la meme cle.
CREATE TABLE IF NOT EXISTS google_signal_notifications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    notified_at      TEXT    NOT NULL,   -- ISO8601 UTC
    origin           TEXT    NOT NULL,
    destination      TEXT    NOT NULL,
    departure_date   TEXT    NOT NULL,
    return_date      TEXT,
    price            REAL    NOT NULL,
    currency         TEXT    NOT NULL,
    price_level      TEXT    NOT NULL,   -- toujours "low" en pratique (seul cas qui declenche l'envoi)
    observation_id   INTEGER NOT NULL REFERENCES flight_observations(id)
);

CREATE INDEX IF NOT EXISTS idx_google_signal_key_time
    ON google_signal_notifications (origin, destination, departure_date, return_date, notified_at);
