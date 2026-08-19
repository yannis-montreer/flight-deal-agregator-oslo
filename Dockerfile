# python:3.12-slim plutot qu'Alpine : evite les frictions de compilation de wheels
# (musl vs glibc) pour httpx/pyyaml. La taille d'image n'est pas un enjeu pour un
# unique conteneur personnel.
FROM python:3.12-slim

WORKDIR /app

# Dependances installees avant de copier le code applicatif : le layer pip install reste
# en cache Docker tant que requirements.txt ne change pas, meme si le code change souvent.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY flightdeals/ ./flightdeals/
COPY config/ ./config/
COPY trip_watch/ ./trip_watch/

# Utilisateur non-root : bonne pratique standard, aucune raison de tourner en root pour
# un process qui ne fait qu'appeler des API HTTP et ecrire dans /data.
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/logs /data/logs-trip-watch \
    && chown -R appuser:appuser /app /data

USER appuser

# Le volume /data est le SEUL etat qui doit survivre a un restart/recreate du conteneur
# (SQLite + logs) — voir docker-compose.yml pour le bind mount correspondant.
VOLUME ["/data"]

ENV FLIGHTDEALS_DB_PATH=/data/flightdeals.db \
    FLIGHTDEALS_LOG_DIR=/data/logs \
    FLIGHTDEALS_CONFIG_PATH=/app/config/config.yaml \
    PYTHONUNBUFFERED=1
# PYTHONUNBUFFERED=1 est important ici : sans lui, stdout est bufferise par bloc (pas par
# ligne) quand il n'est pas attache a un TTY — `docker logs -f` pourrait retarder de plusieurs
# minutes l'affichage sur un process a faible debit de sortie comme celui-ci (~1 log/jour).

# Image partagee par les 2 services (flightdeals + trip_watch, voir docker-compose.yml) :
# le service trip_watch surcharge cet ENTRYPOINT via `entrypoint: [...]` (PAS `command:`,
# qui s'ajouterait a celui-ci au lieu de le remplacer) plutot que de dupliquer un 2e
# Dockerfile/build pour un si petit outil annexe.
ENTRYPOINT ["python", "-m", "flightdeals"]
