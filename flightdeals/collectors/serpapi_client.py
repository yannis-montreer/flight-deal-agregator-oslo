"""Client SerpApi minimal : authentification, timeout, retry/backoff, exceptions typees.

Un seul point d'appel HTTP centralise (search()) pour que le comptage de requetes
(voir flightdeals.db.repository.increment_requests_used, appele par pipeline.py au jalon M8)
et la gestion d'erreurs soient coherents partout dans le projet.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://serpapi.com/search.json"
ACCOUNT_URL = "https://serpapi.com/account.json"

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_JITTER_SECONDS = 0.5


class SerpApiError(Exception):
    """Erreur SerpApi generique : reponse invalide, erreur serveur persistante, parametre
    invalide, etc."""


class QuotaExceededError(SerpApiError):
    """Quota mensuel depasse. Ne JAMAIS retry cette erreur specifiquement — reessayer ne fait
    qu'aggraver la situation. pipeline.py (jalon M8) doit l'attraper specifiquement pour
    arreter les appels SerpApi restants du jour sans faire echouer tout le run."""


class SerpApiClient:
    def __init__(self, api_key: str, timeout_seconds: float = 30.0) -> None:
        self._api_key = api_key
        self._client = httpx.Client(timeout=httpx.Timeout(timeout_seconds))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SerpApiClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_account_info(self) -> dict[str, Any]:
        """Ne consomme PAS de quota (endpoint gratuit de SerpApi) — a appeler en debut de run
        pour verifier le budget restant avant le moindre appel search()."""
        response = self._client.get(ACCOUNT_URL, params={"api_key": self._api_key})
        response.raise_for_status()
        return response.json()

    def search(self, params: dict[str, Any]) -> dict[str, Any]:
        """Un appel = 1 requete du quota mensuel, que la reponse soit un succes ou une erreur.

        Retry avec backoff exponentiel + jitter sur erreurs transitoires (timeout, 5xx).
        Jamais de retry sur quota depasse (429, ou champ "error" mentionnant le quota dans un
        200) : SerpApi peut renvoyer l'un ou l'autre selon le cas, les deux sont geres.
        """
        full_params = {**params, "api_key": self._api_key}

        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = self._client.get(BASE_URL, params=full_params)
            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning("SerpApi timeout (tentative %d/%d): %s", attempt, _MAX_RETRIES, exc)
                if attempt < _MAX_RETRIES:
                    self._sleep_backoff(attempt)
                continue

            if response.status_code == 429:
                raise QuotaExceededError(f"SerpApi quota depasse (HTTP 429): {response.text[:300]}")

            if response.status_code >= 500:
                last_exc = SerpApiError(f"Erreur serveur SerpApi ({response.status_code})")
                logger.warning(
                    "SerpApi HTTP %d (tentative %d/%d), retry...", response.status_code, attempt, _MAX_RETRIES
                )
                if attempt < _MAX_RETRIES:
                    self._sleep_backoff(attempt)
                continue

            if response.status_code >= 400:
                # 4xx hors 429 : erreur de requete (parametre invalide, etc.) — pas transitoire, pas de retry
                raise SerpApiError(f"SerpApi a renvoye HTTP {response.status_code}: {response.text[:300]}")

            data = response.json()
            error = data.get("error")
            if error:
                error_lower = str(error).lower()
                if "quota" in error_lower or "run out of searches" in error_lower:
                    raise QuotaExceededError(f"SerpApi quota depasse: {error}")
                raise SerpApiError(f"SerpApi a renvoye une erreur: {error}")

            return data

        raise SerpApiError(f"SerpApi injoignable apres {_MAX_RETRIES} tentatives") from last_exc

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        delay = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, _BACKOFF_JITTER_SECONDS)
        time.sleep(delay)
