"""
Phase 5 — Détection des signaux (v2) — calibrée sur les deep tests S3
=======================================================================
Améliorations issues du deep test S3 (backtest_s3_deep.py) :

  Volume tiers (T2)  : win_rate_prior S3 ajusté par volume du marché
                        Vol > 20K$ → 99.9%  |  Vol 5-20K$ → 98.1%
                        Vol 1-5K$  → 97.7%  |  Vol < 1K$  → 99.2%

  Catégories (T6)    : Politique US/Monde → +1.5pp  |  Tech → +0.5pp

  Durée (T3)         : 30-90 jours → +1.5pp (WR 100% observé)

  Sweetspot (T1)     : YES 6-7% → +0.5pp (Sharpe 14.07, meilleur bucket)

  Priorité signaux   : score 0-10 pour trier quand le capital est limité
"""

import sys
from datetime import datetime, timezone
from typing import Optional

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Constantes ────────────────────────────────────────────────────────────────

CRYPTO_KEYWORDS = [
    "bitcoin", " btc", "ethereum", " eth", "solana", "xrp", "crypto",
    "up or down", "updown",
]

IMPOSSIBLE_KEYWORDS = [
    "jesus", "second coming", "rapture",
    "alien", "aliens exist", "ufo confirmed", "extraterrestrial confirmed",
    "world war iii", "world war 3", "wwiii", "ww3", "nuclear war",
    "end of the world", "apocalypse", "asteroid hits",
    "zombie", "flat earth confirmed",
    "time travel", "teleportation confirmed",
]

# Mots-clés catégories (T6)
_KW_POL_US = ["president", "trump", "biden", "harris", "congress", "senate",
              "democrat", "republican", "white house", "governor", "midterm"]
_KW_POL_WO = ["parliament", "prime minister", "chancellor", "ceasefire",
              "nato", "sanction", "coup", "invasion", "election"]
_KW_SPORT  = ["nfl", "nba", "mlb", "nhl", "fifa", "world cup", "champion",
              "super bowl", "playoffs", "finals", "ufc", "boxing", "tennis",
              "formula 1", "f1", "grand prix", "match", "league", "tournament"]
_KW_TECH   = ["ai", "openai", "gpt", "apple", "google", "meta", "microsoft",
              "amazon", "tesla", "spacex", "elon", "nvidia", "chatgpt"]
_KW_ECON   = ["fed", "inflation", "gdp", "recession", "interest rate",
              "nasdaq", "s&p 500", "dow jones", "ipo", "bankruptcy"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_crypto(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in CRYPTO_KEYWORDS)


def _is_impossible(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in IMPOSSIBLE_KEYWORDS)


def _category(question: str) -> str:
    q = question.lower()
    if any(kw in q for kw in _KW_POL_US):  return "politics_us"
    if any(kw in q for kw in _KW_POL_WO):  return "politics_world"
    if any(kw in q for kw in _KW_SPORT):   return "sport"
    if any(kw in q for kw in _KW_TECH):    return "tech"
    if any(kw in q for kw in _KW_ECON):    return "economy"
    return "other"


def _days_since_creation(market: dict) -> Optional[float]:
    created = market.get("createdAt") or market.get("created_at")
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        return (datetime.now(tz=timezone.utc) - dt).total_seconds() / 86400
    except (ValueError, AttributeError):
        return None


def _market_duration_days(market: dict) -> Optional[float]:
    """Durée totale du marché (created_at → end_date)."""
    created = market.get("createdAt") or market.get("created_at")
    end     = market.get("endDate")   or market.get("end_date")
    if not created or not end:
        return None
    try:
        dt_c = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        dt_e = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
        return (dt_e - dt_c).total_seconds() / 86400
    except (ValueError, AttributeError):
        return None


# ── Win rate prior dynamique pour S3 ──────────────────────────────────────────

def _s3_prior(yes_price: float, volume: float, question: str,
              market: dict) -> tuple[float, str]:
    """
    Calcule le win_rate_prior S3 et le score de confiance (0-10).

    Retourne (prior, reason_detail).

    Bases issues du deep test T2 (volume) :
      Vol < 1K$   : 99.2%   |  Vol 1-5K$  : 97.7%
      Vol 5-20K$  : 98.1%   |  Vol 20K$+  : 99.9%

    Bonus additionnel par catégorie (T6), durée (T3), sweetspot prix (T1).
    """
    details = []

    # Base volume (T2)
    if volume >= 20_000:
        base = 0.999; details.append(f"vol={volume/1000:.0f}K$→99.9%")
    elif volume >= 5_000:
        base = 0.981; details.append(f"vol={volume/1000:.0f}K$→98.1%")
    elif volume >= 1_000:
        base = 0.977; details.append(f"vol={volume/1000:.1f}K$→97.7%")
    else:
        base = 0.992; details.append(f"vol={volume:.0f}$→99.2%")

    bonus = 0.0

    # Bonus catégorie (T6)
    cat = _category(question)
    if cat in ("politics_us", "politics_world"):
        bonus += 0.015; details.append(f"{cat}→+1.5pp")
    elif cat == "tech":
        bonus += 0.005; details.append("tech→+0.5pp")

    # Bonus durée 30-90 jours (T3 : WR 100%)
    dur = _market_duration_days(market)
    if dur is not None and 30 <= dur <= 90:
        bonus += 0.015; details.append(f"dur={dur:.0f}j→+1.5pp")

    # Bonus sweetspot prix 6-7% (T1 : Sharpe 14.07)
    if 0.060 <= yes_price <= 0.070:
        bonus += 0.005; details.append("sweetspot_6-7%→+0.5pp")

    prior = min(base + bonus, 0.999)

    # Score de confiance 0-10 (pour tri quand capital limité)
    score = 5.0
    if volume >= 20_000: score += 2
    elif volume >= 5_000: score += 1
    if cat in ("politics_us", "politics_world"): score += 2
    elif cat == "tech": score += 0.5
    if dur is not None and 30 <= dur <= 90: score += 1
    if 0.060 <= yes_price <= 0.070: score += 0.5
    score = min(score, 10.0)

    reason = f"YES={yes_price:.3f}, " + ", ".join(details) + f" → prior={prior:.4f}, score={score:.1f}"
    return prior, reason, score


# ── Signal principal ───────────────────────────────────────────────────────────

def check_signals(market: dict, yes_price: float) -> list[dict]:
    """
    Analyse un marché live et retourne les signaux déclenchés.

    Retourne une liste de dicts :
        [{
          "strategy":       "S3",
          "win_rate_prior": 0.981,
          "reason":         "...",
          "score":          7.5,      # confiance 0-10 pour prioritisation
        }, ...]
    """
    signals = []
    question = str(market.get("question", ""))
    volume   = float(market.get("volume", 0) or 0)

    # ── S5 : Impossible events ────────────────────────────────────────────────
    if _is_impossible(question):
        signals.append({
            "strategy":       "S5",
            "win_rate_prior": 0.999,
            "reason":         f"Mot-cle impossible, YES={yes_price:.3f}",
            "score":          9.0,
        })

    is_crypto = _is_crypto(question)

    # ── S2 : YES < 5% ─────────────────────────────────────────────────────────
    if not is_crypto and 0.001 <= yes_price < 0.05:
        # Score fonction du prix (plus c'est bas, plus c'est sûr)
        score_s2 = 8.0 + (0.05 - yes_price) * 40  # 0.01% → 9.6, 0.04% → 8.4
        signals.append({
            "strategy":       "S2",
            "win_rate_prior": 0.999,
            "reason":         f"YES={yes_price:.3f} < 5%",
            "score":          min(score_s2, 10.0),
        })

    # ── S3 : YES 5-10% (prior dynamique) ─────────────────────────────────────
    elif not is_crypto and 0.05 <= yes_price <= 0.10:
        prior, reason, score = _s3_prior(yes_price, volume, question, market)
        signals.append({
            "strategy":       "S3",
            "win_rate_prior": prior,
            "reason":         reason,
            "score":          score,
        })

    # ── S4 : YES 10-20% ───────────────────────────────────────────────────────
    elif not is_crypto and 0.10 < yes_price <= 0.20:
        score_s4 = 5.0 + (0.20 - yes_price) * 20  # 11% → 6.8, 19% → 5.2
        signals.append({
            "strategy":       "S4",
            "win_rate_prior": 0.940,
            "reason":         f"YES={yes_price:.3f} entre 10-20%",
            "score":          score_s4,
        })

    # ── S1 : Biais NO Global ──────────────────────────────────────────────────
    # Seulement hors S2/S3/S4/S5, vol ≥ 5K$, politique ou volume élevé
    if not is_crypto and 0.20 < yes_price <= 0.95 and volume >= 5_000:
        if not any(s["strategy"] in ("S2", "S3", "S4", "S5") for s in signals):
            cat = _category(question)
            # Score plus élevé pour les catégories à WR confirmé
            score_s1 = 3.0
            if cat in ("politics_us", "politics_world"): score_s1 += 2
            if volume >= 50_000: score_s1 += 1
            signals.append({
                "strategy":       "S1",
                "win_rate_prior": 0.672,
                "reason":         f"YES={yes_price:.3f}, {cat}, vol={volume:.0f}$",
                "score":          score_s1,
            })

    # ── S6 : Long duration + faible volume ────────────────────────────────────
    if not is_crypto and 0.05 <= yes_price <= 0.95 and volume < 10_000:
        age_days = _days_since_creation(market)
        if age_days is not None and age_days >= 30:
            signals.append({
                "strategy":       "S6",
                "win_rate_prior": 0.830,
                "reason":         f"Age={age_days:.0f}j, vol={volume:.0f}$",
                "score":          4.0,
            })

    return signals
