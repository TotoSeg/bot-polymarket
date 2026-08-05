"""
Phase 5 — Signaux S3 + SP
==========================
S3  : YES 5-10%, tous marchés hors crypto. WR 99.7%, ROI +1495% sur 2024-25.
SP  : YES 5-35%, marchés politiques/géopolitiques. WR 94.9%, ROI +1522% sur 2024-25.

Les deux stratégies sont indépendantes et peuvent coexister sur le même marché
(un marché politique YES=7% déclenche S3 ET SP).
"""

import sys
from datetime import datetime, timezone
from typing import Optional

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Mots-clés ─────────────────────────────────────────────────────────────────

_KW_CRYPTO = ["bitcoin", " btc", "ethereum", " eth", "solana", "xrp", "crypto",
              "up or down", "updown"]

_KW_POL = [
    "trump", "biden", "harris", "election", "congress", "senate", "president",
    "democrat", "republican", "white house", "governor", "midterm", "parliament",
    "prime minister", "chancellor", "ceasefire", "nato", "sanction", "coup",
    "invasion", "referendum", "vote", "tariff", "legislation", "war", "treaty",
    "nuclear", "troops", "geopolit", "diplomacy",
]

_KW_POL_US = ["president", "trump", "biden", "harris", "congress", "senate",
              "democrat", "republican", "white house", "governor", "midterm"]
_KW_POL_WO = ["parliament", "prime minister", "chancellor", "ceasefire",
              "nato", "sanction", "coup", "invasion", "election"]
_KW_TECH   = ["ai", "openai", "gpt", "apple", "google", "meta", "microsoft",
              "amazon", "tesla", "spacex", "elon", "nvidia", "chatgpt"]


def _is_crypto(q: str) -> bool:
    return any(k in q for k in _KW_CRYPTO)


def _is_political(q: str) -> bool:
    return any(k in q for k in _KW_POL)


def _category(q: str) -> str:
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


# ── S3 ────────────────────────────────────────────────────────────────────────

def _s3_signal(market: dict, yes_price: float, question_lc: str) -> Optional[dict]:
    """S3 : YES 5-10%, hors crypto. WR prior dynamique selon volume/catégorie/durée."""
    if not (0.05 <= yes_price <= 0.10):
        return None

    volume = float(market.get("volume", 0) or 0)

    if volume >= 20_000:   base = 0.999; vd = f"vol={volume/1000:.0f}K->99.9%"
    elif volume >= 5_000:  base = 0.981; vd = f"vol={volume/1000:.0f}K->98.1%"
    elif volume >= 1_000:  base = 0.977; vd = f"vol={volume/1000:.1f}K->97.7%"
    else:                  base = 0.992; vd = f"vol={volume:.0f}->99.2%"

    bonus = 0.0; details = [vd]; score = 5.0

    cat = _category(question_lc)
    if cat in ("politics_us", "politics_world"):
        bonus += 0.015; details.append(f"{cat}->+1.5pp"); score += 2
    elif cat == "tech":
        bonus += 0.005; details.append("tech->+0.5pp"); score += 0.5

    dur = _market_duration_days(market)
    if dur is not None and 30 <= dur <= 90:
        bonus += 0.015; details.append(f"dur={dur:.0f}j->+1.5pp"); score += 1

    if 0.060 <= yes_price <= 0.070:
        bonus += 0.005; details.append("sweetspot->+0.5pp"); score += 0.5

    if volume >= 20_000: score += 2
    elif volume >= 5_000: score += 1

    prior = min(base + bonus, 0.999)
    score = min(score, 10.0)

    return {
        "strategy":       "S3",
        "win_rate_prior": prior,
        "reason":         f"YES={yes_price:.3f}, " + ", ".join(details) + f" prior={prior:.4f}",
        "score":          score,
    }


# ── SP ────────────────────────────────────────────────────────────────────────

def _sp_signal(market: dict, yes_price: float, question_lc: str,
               is_neg_risk: bool = False) -> Optional[dict]:
    """
    SP : YES 10-25% (NO 75-90%).
    - Marchés politiques/géopolitiques : WR backtest 92.2-99.2%
    - Marchés negRisk (brackets mutuellement exclusifs) : même structure d'edge
      (1 seul bracket résout YES, tous les autres NO → taux de réussite élevé).
      Prior légèrement réduit vs marchés politiques (pas de backtest historique).
    """
    if not (0.10 <= yes_price <= 0.25):
        return None

    if yes_price < 0.20:
        prior = 0.985 if is_neg_risk else 0.992
        score = 6.5  if is_neg_risk else 7.0
        tier  = "10-20%->98.5%(neg-risk)" if is_neg_risk else "10-20%->99.2%"
    else:
        prior = 0.900 if is_neg_risk else 0.922
        score = 5.5  if is_neg_risk else 6.0
        tier  = "20-25%->90%(neg-risk)" if is_neg_risk else "20-25%->92.2%"

    cat = _category(question_lc)
    if cat == "politics_us":
        score += 1.5; tier += ",pol_US"
    elif cat == "politics_world":
        score += 1.0; tier += ",pol_WO"
    elif cat == "tech" and is_neg_risk:
        score += 0.5; tier += ",tech_neg-risk"

    volume = float(market.get("volume", 0) or 0)
    if volume >= 20_000: score += 1.0
    elif volume >= 5_000: score += 0.5

    score = min(score, 10.0)

    return {
        "strategy":       "SP",
        "win_rate_prior": prior,
        "reason":         f"YES={yes_price:.3f}, {tier}",
        "score":          score,
    }


# ── Point d'entrée ────────────────────────────────────────────────────────────

def check_signals(market: dict, yes_price: float) -> list[dict]:
    """
    Analyse un marché live et retourne les signaux déclenchés (S3 et/ou SP).

    SP s'applique aux marchés politiques ET aux marchés negRisk (brackets) :
    même edge structurel — au plus un bracket résout YES, tous les autres NON.
    """
    signals = []
    question = str(market.get("question", ""))
    question_lc = question.lower()
    is_neg_risk = bool(market.get("_event_id") or market.get("negRisk"))

    if _is_crypto(question_lc):
        return []

    sig_s3 = _s3_signal(market, yes_price, question_lc)
    if sig_s3:
        signals.append(sig_s3)

    if _is_political(question_lc) or is_neg_risk:
        sig_sp = _sp_signal(market, yes_price, question_lc, is_neg_risk=is_neg_risk)
        if sig_sp:
            signals.append(sig_sp)

    return signals
