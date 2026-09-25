"""Déduplication en 3 étages (§1 et §2 du cahier des charges) :

1. Canonicalisation d'URL — deux URLs qui ne diffèrent que par un paramètre
   de tracking (`utm_*`, fragment) pointent vers la même source.
2. Empreinte de contenu — deux textes identiques (à la casse/espaces près)
   ont la même empreinte, même si l'URL diffère.
3. Similarité lexicale + règles — jamais un merge automatique et silencieux
   d'opportunités : seulement une PROPOSITION, qui exige que l'acheteur et
   le secteur se recoupent (pas seulement le vocabulaire), et qui reste
   soumise à confirmation humaine au-dessus du seuil « revue ».
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

_PARAMS_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "fbclid", "gclid"}

SEUIL_FUSION_AUTO = 0.85   # + secteur ET acheteur identiques -> même cluster sans intervention
SEUIL_REVUE = 0.55         # au-dessus : proposé à la revue humaine, jamais fusionné seul


def canonicaliser_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _PARAMS_TRACKING]
    chemin = parts.path.rstrip("/") or "/"
    netloc = parts.netloc.lower()
    return urlunsplit((parts.scheme.lower(), netloc, chemin, urlencode(sorted(query)), ""))


def empreinte_contenu(texte: str) -> str:
    normalise = re.sub(r"\s+", " ", texte.strip().lower())
    return hashlib.sha256(normalise.encode("utf-8")).hexdigest()


def _tokeniser(texte: str) -> list[str]:
    return re.findall(r"[a-zàâäéèêëïîôöùûüç0-9]+", texte.lower())


def similarite_lexicale(a: str, b: str) -> float:
    """Cosinus sur sacs de mots, sans dépendance externe — suffisant pour un
    volume nocturne de quelques centaines de signaux."""
    tokens_a, tokens_b = _tokeniser(a), _tokeniser(b)
    if not tokens_a or not tokens_b:
        return 0.0
    from collections import Counter

    ca, cb = Counter(tokens_a), Counter(tokens_b)
    communs = set(ca) & set(cb)
    produit_scalaire = sum(ca[t] * cb[t] for t in communs)
    norme_a = sum(v * v for v in ca.values()) ** 0.5
    norme_b = sum(v * v for v in cb.values()) ** 0.5
    if norme_a == 0 or norme_b == 0:
        return 0.0
    return produit_scalaire / (norme_a * norme_b)


def _acheteurs_compatibles(acheteur_a: str, acheteur_b: str) -> bool:
    """Règle, pas de sentiment : au moins un token significatif (3+ lettres)
    en commun entre les deux descriptions d'acheteur."""
    ta = {t for t in _tokeniser(acheteur_a) if len(t) >= 3}
    tb = {t for t in _tokeniser(acheteur_b) if len(t) >= 3}
    return bool(ta & tb)


@dataclass
class SuggestionCluster:
    opportunity_id: str
    similarite: float
    fusion_automatique: bool  # True seulement si secteur+acheteur compatibles ET similarité >= seuil fort


def proposer_cluster(
    *, secteur: str, acheteur: str, texte: str,
    existantes: list[dict],  # chacune: {id, secteur, acheteur, probleme}
) -> SuggestionCluster | None:
    meilleure: SuggestionCluster | None = None
    for opp in existantes:
        if opp["secteur"] != secteur:
            continue  # jamais de fusion entre secteurs différents sur le seul vocabulaire
        if not _acheteurs_compatibles(acheteur, opp["acheteur"]):
            continue
        sim = similarite_lexicale(texte, opp["probleme"])
        if sim < SEUIL_REVUE:
            continue
        candidate = SuggestionCluster(
            opportunity_id=opp["id"],
            similarite=sim,
            fusion_automatique=sim >= SEUIL_FUSION_AUTO,
        )
        if meilleure is None or candidate.similarite > meilleure.similarite:
            meilleure = candidate
    return meilleure
