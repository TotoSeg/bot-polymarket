"""
Phase 5 — Détection des signaux S3 uniquement
===============================================
S3 : YES entre 5 % et 10 %, résolution NO dans 98.1 % des cas.

Win rate prior dynamique selon volume + catégorie + durée + sweetspot prix.
Score 0-10 pour trier quand le capital est limité.
"""

import sys
from datetime import datetime, timezone
from typing import Optional

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_KW_POL_US = ["president", "trump", "biden", "harris", "congress", "senate",
              "democrat", "republican", "white house", "governor", "midterm"]
_KW_POL_WO = ["parliament", "prime minister", "chancellor", "ceasefire",
              "nato", "sanction", "coup", "invasion", "election"]
_KW_TECH   = ["ai", "openai", "gpt", "apple", "google", "meta", "microsoft",
              "amazon", "tesla", "spacex", "elon", "nvidia", "chatgpt"]
_KW_CRYPTO = ["bitcoin", " btc", "ethereum", " eth", "solana", "xrp", "crypto",
              "up or down", "updown"]


def _category(q: str) -> str:
    q = q.lower()
    if any(k in q for k in _KW_POL_US): return "politics_us"
    if any(k in q for k in _KW_POL_WO): return "politics_world"
    if any(k in q for k in _KW_TECH):   return "tech"
    return "other"


def _market_duration_days(market: dict) -> Optional[float]:
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


def check_signals(market: dict, yes_price: float) -> list[dict]:
    """
    Retourne les signaux S3 pour un marché donné.
    Exclut les marchés crypto (pas de signal S3 sur BTC/ETH Up or Down).
    """
    question = str(market.get("question", ""))
    if any(k in question.lower() for k in _KW_CRYPTO):
        return []
    if not (0.05 <= yes_price <= 0.10):
        return []

    volume = float(market.get("volume", 0) or 0)

    # Base win rate par volume (backtest T2)
    if volume >= 20_000:   base = 0.999; vd = f"vol={volume/1000:.0f}K$→99.9%"
    elif volume >= 5_000:  base = 0.981; vd = f"vol={volume/1000:.0f}K$→98.1%"
    elif volume >= 1_000:  base = 0.977; vd = f"vol={volume/1000:.1f}K$→97.7%"
    else:                  base = 0.992; vd = f"vol={volume:.0f}$→99.2%"

    bonus = 0.0; details = [vd]; score = 5.0

    cat = _category(question)
    if cat in ("politics_us", "politics_world"):
        bonus += 0.015; details.append(f"{cat}→+1.5pp"); score += 2
    elif cat == "tech":
        bonus += 0.005; details.append("tech→+0.5pp"); score += 0.5

    dur = _market_duration_days(market)
    if dur is not None and 30 <= dur <= 90:
        bonus += 0.015; details.append(f"dur={dur:.0f}j→+1.5pp"); score += 1

    if 0.060 <= yes_price <= 0.070:
        bonus += 0.005; details.append("sweetspot→+0.5pp"); score += 0.5

    if volume >= 20_000: score += 2
    elif volume >= 5_000: score += 1

    prior = min(base + bonus, 0.999)
    score = min(score, 10.0)

    return [{
        "strategy":       "S3",
        "win_rate_prior": prior,
        "reason":         f"YES={yes_price:.3f}, " + ", ".join(details) + f" → prior={prior:.4f}",
        "score":          score,
    }]
