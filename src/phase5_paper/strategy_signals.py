"""
Phase 5 — Détection des signaux de trading en temps réel
==========================================================
Applique les mêmes filtres que le backtest (phase 4) sur les marchés live.

Stratégies supportées :
  S1 - Biais NO Global          : tout marché non-crypto, YES entre 5-95%
  S2 - Nothing Ever Happens <5% : YES < 5%
  S3 - Nothing Ever Happens 5-10% : YES entre 5-10%
  S4 - Nothing Ever Happens 10-20% : YES entre 10-20%
  S5 - Événements impossibles   : mots-clés Jesus/Aliens/WW3/etc.
  S6 - Long duration + low vol  : ouvert > 30 jours, volume < 10K$

Pour chaque marché, check_signals() retourne la liste des stratégies déclenchées
avec le win_rate_prior correspondant (issu de la phase 3).
"""

import sys
from datetime import datetime, timezone
from typing import Optional

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Mots-clés pour exclure les marchés crypto de S1
CRYPTO_KEYWORDS = [
    "bitcoin", " btc", "ethereum", " eth", "solana", "xrp", "crypto",
    "up or down", "updown",
]

# Mots-clés pour les événements physiquement impossibles (S5)
IMPOSSIBLE_KEYWORDS = [
    "jesus", "second coming", "rapture",
    "alien", "aliens exist", "ufo confirmed", "extraterrestrial confirmed",
    "world war iii", "world war 3", "wwiii", "ww3", "nuclear war",
    "end of the world", "apocalypse", "asteroid hits",
    "zombie", "flat earth confirmed",
    "time travel", "teleportation confirmed",
]

# Win rates historiques issus du backtest phase 3 (pour Kelly criterion)
WIN_RATE_PRIORS = {
    "S1": 0.672,
    "S2": 0.999,
    "S3": 0.975,
    "S4": 0.940,
    "S5": 0.999,
    "S6": 0.830,
}


def _is_crypto(question: str) -> bool:
    """Retourne True si la question concerne les cryptos."""
    q = question.lower()
    return any(kw in q for kw in CRYPTO_KEYWORDS)


def _is_impossible(question: str) -> bool:
    """Retourne True si la question contient un mot-clé 'impossible'."""
    q = question.lower()
    return any(kw in q for kw in IMPOSSIBLE_KEYWORDS)


def _days_since_creation(market: dict) -> Optional[float]:
    """Retourne le nombre de jours depuis la création du marché."""
    created = market.get("createdAt") or market.get("created_at")
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        now = datetime.now(tz=timezone.utc)
        return (now - dt).total_seconds() / 86400
    except (ValueError, AttributeError):
        return None


def check_signals(market: dict, yes_price: float) -> list[dict]:
    """
    Analyse un marché live et retourne les signaux déclenchés.

    market    : dict d'un marché Polymarket (depuis l'API Gamma)
    yes_price : prix YES courant (entre 0 et 1)

    Retourne une liste de dicts :
        [{"strategy": "S2", "win_rate_prior": 0.999, "reason": "..."}, ...]
    Retourne [] si aucun signal.
    """
    signals = []
    question = str(market.get("question", ""))
    volume   = float(market.get("volume", 0) or 0)

    # ── S5 : Événements impossibles ───────────────────────────────────────────
    # Priorité maximale — traité en premier
    if _is_impossible(question):
        signals.append({
            "strategy":       "S5",
            "win_rate_prior": WIN_RATE_PRIORS["S5"],
            "reason":         f"Mot-cle impossible detecte, YES={yes_price:.3f}",
        })

    # ── Filtre crypto (S1/S2/S3/S4/S6 excluent le crypto) ────────────────────
    is_crypto = _is_crypto(question)

    # ── S2 : YES < 5% ─────────────────────────────────────────────────────────
    if not is_crypto and 0.001 <= yes_price < 0.05:
        signals.append({
            "strategy":       "S2",
            "win_rate_prior": WIN_RATE_PRIORS["S2"],
            "reason":         f"YES={yes_price:.3f} < 5%",
        })

    # ── S3 : YES 5-10% ────────────────────────────────────────────────────────
    elif not is_crypto and 0.05 <= yes_price <= 0.10:
        signals.append({
            "strategy":       "S3",
            "win_rate_prior": WIN_RATE_PRIORS["S3"],
            "reason":         f"YES={yes_price:.3f} entre 5-10%",
        })

    # ── S4 : YES 10-20% ───────────────────────────────────────────────────────
    elif not is_crypto and 0.10 < yes_price <= 0.20:
        signals.append({
            "strategy":       "S4",
            "win_rate_prior": WIN_RATE_PRIORS["S4"],
            "reason":         f"YES={yes_price:.3f} entre 10-20%",
        })

    # ── S1 : Biais NO Global (hors crypto, hors S2/S3/S4/S5) ─────────────────
    # Seulement si aucune autre stratégie N/E plus précise n'a été déclenchée
    # et volume minimum pour garantir la liquidité
    if not is_crypto and 0.20 < yes_price <= 0.95 and volume >= 5000:
        if not any(s["strategy"] in ("S2", "S3", "S4", "S5") for s in signals):
            signals.append({
                "strategy":       "S1",
                "win_rate_prior": WIN_RATE_PRIORS["S1"],
                "reason":         f"YES={yes_price:.3f}, vol={volume:.0f}$",
            })

    # ── S6 : Long duration + faible volume ────────────────────────────────────
    if not is_crypto and volume < 10_000:
        age_days = _days_since_creation(market)
        if age_days is not None and age_days >= 30:
            signals.append({
                "strategy":       "S6",
                "win_rate_prior": WIN_RATE_PRIORS["S6"],
                "reason":         f"Age={age_days:.0f}j, vol={volume:.0f}$",
            })

    return signals
