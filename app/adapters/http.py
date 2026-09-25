"""Appel HTTP avec timeout, retry plafonné et backoff — utilisé par tous les
adaptateurs réseau, jamais d'appel direct à `requests` ailleurs."""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)


class ErreurCollecte(Exception):
    pass


def get_with_retry(url: str, *, max_retries: int = 3, base_delay: float = 2.0, timeout: float = 10.0) -> requests.Response:
    derniere_erreur: Exception | None = None
    for tentative in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "radar-opportunites-ia/0.1 (labo-ia)"})
            if resp.status_code == 429:
                delai = base_delay * (2 ** (tentative - 1))
                logger.warning("429 reçu pour %s, backoff %.1fs (tentative %d/%d)", url, delai, tentative, max_retries)
                time.sleep(delai)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            derniere_erreur = exc
            if tentative < max_retries:
                delai = base_delay * (2 ** (tentative - 1))
                time.sleep(delai)
    raise ErreurCollecte(f"Échec après {max_retries} tentatives pour {url}: {derniere_erreur}")
