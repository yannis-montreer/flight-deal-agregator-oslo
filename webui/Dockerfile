# Image de l'interface d'edition, separee de l'image des workers : ses dependances web
# (FastAPI, uvicorn...) ne touchent pas l'image qui fait tourner les veilles. Contexte de
# build = racine du depot (voir docker-compose.yml), pour reutiliser les chargeurs de config
# de flightdeals/ et trip_watch/ (meme validation que les services eux-memes).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./requirements.txt
COPY webui/requirements.txt ./webui-requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r webui-requirements.txt

COPY flightdeals/ ./flightdeals/
COPY trip_watch/ ./trip_watch/
COPY webui/ ./webui/

RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 \
    WEBUI_HOST=0.0.0.0 \
    WEBUI_PORT=8765

ENTRYPOINT ["python", "-m", "webui"]
