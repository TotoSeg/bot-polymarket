"""
Stratégie SX — Short X Tweets
==============================
Se positionne NO sur les brackets de tweets improbables sur Polymarket,
en utilisant l'API xtracker.polymarket.com (oracle de résolution officiel).

Critères d'entrée :
  1. Marché se terminant dans ≤ 48h (SX_MAX_HOURS)
  2. Zscore saisonnier du bracket ≥ SX_ZSCORE_MIN (2.5)
     zscore = (borne_inférieure_bracket − moyenne_saisonnière) / σ_saisonnier
  3. Bonus pace si ≤ 24h : tweets restants nécessaires >> pace historique

Kelly au sein d'un même marché :
  - Classer les brackets candidats par zscore décroissant
  - 1er bracket : Kelly plein (×1.0)
  - Nème bracket : Kelly × (1 / √N)

Sources de données :
  - xtracker.polymarket.com : historique tweets + marchés actifs
  - gamma-api.polymarket.com : sous-marchés (brackets) + prix courants
"""

import re
import json
import math
import time
import requests
from datetime import datetime, timezone, timedelta
from statistics import mean, stdev
from typing import Optional

from loguru import logger

XTRACKER_API      = "https://xtracker.polymarket.com/api"
GAMMA_API         = "https://gamma-api.polymarket.com"

SX_MAX_HOURS      = 48    # Horizon maximum d'entrée
SX_ZSCORE_MIN     = 2.0   # Zscore saisonnier minimum pour entrer (2.5 trop strict en pratique)
SX_MIN_SEASONAL   = 4     # Nombre minimum de fenêtres pour la moyenne saisonnière
SX_PACE_BONUS     = 1.2   # Multiplicateur zscore si pace confirme l'improbabilité
SX_LOOKBACK_WEEKS = 52    # Semaines d'historique à charger
REQUEST_DELAY     = 0.2


# ── Helper champs défensif ────────────────────────────────────────────────────

def _get_field(obj: dict, *keys, default=""):
    """Retourne la première valeur non-None trouvée parmi les clés candidates.
    Protège contre les renommages silencieux de l'API xtracker (isActive/active, etc.)."""
    for k in keys:
        v = obj.get(k)
        if v is not None:
            return v
    return default


# ── Helpers HTTP ──────────────────────────────────────────────────────────────

def _xget(path: str, params: dict = None):
    try:
        r = requests.get(f"{XTRACKER_API}{path}", params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"[SX] xtracker {path} : {e}")
        return None


def _gamma_get(path: str, params: dict = None):
    try:
        r = requests.get(f"{GAMMA_API}{path}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug(f"[SX] Gamma {path} : {e}")
        return None


def _unwrap(data) -> list:
    """Extrait la liste depuis list | {data: [...]} | {success, data: [...]}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("data", [])
    return []


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ── Données historiques ───────────────────────────────────────────────────────

def _get_daily_counts(handle: str) -> dict:
    """
    Charge tous les posts du compte sur les SX_LOOKBACK_WEEKS dernières semaines.
    Retourne {date_str: nb_tweets} en un seul appel API.
    """
    start = (datetime.now(tz=timezone.utc) - timedelta(weeks=SX_LOOKBACK_WEEKS)).strftime("%Y-%m-%d")
    end   = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    data  = _xget(f"/users/{handle}/posts", {"startDate": start, "endDate": end})
    posts = _unwrap(data)

    daily: dict = {}
    for p in posts:
        d = str(p.get("createdAt", ""))[:10]
        if d:
            daily[d] = daily.get(d, 0) + 1
    return daily


def _sum_window(daily: dict, start_dt: datetime, end_dt: datetime) -> int:
    """Somme les tweets journaliers dans une fenêtre [start, end]."""
    total = 0
    cur   = start_dt.date()
    stop  = end_dt.date()
    while cur <= stop:
        total += daily.get(str(cur), 0)
        cur   += timedelta(days=1)
    return total


def _get_historical_counts(handle: str) -> list:
    """
    Construit l'historique des tweet counts par fenêtre de marché.
    Retourne [{"start_dt", "end_dt", "count", "month"}]

    Stratégie efficace : un seul appel posts (large fenêtre) + un appel trackings,
    puis agrégation locale — évite N×162 appels API.
    """
    daily     = _get_daily_counts(handle)
    time.sleep(REQUEST_DELAY)

    data      = _xget(f"/users/{handle}/trackings", {"activeOnly": "false"})
    trackings = _unwrap(data)
    now       = datetime.now(tz=timezone.utc)

    results = []
    for t in trackings:
        if _get_field(t, "isActive", "active", default=False):
            continue
        start_dt = _parse_dt(_get_field(t, "startDate", "start_date", "startAt"))
        end_dt   = _parse_dt(_get_field(t, "endDate", "end_date", "endAt"))
        if not start_dt or not end_dt or end_dt >= now:
            continue

        count = _sum_window(daily, start_dt, end_dt)
        results.append({
            "start_dt": start_dt,
            "end_dt":   end_dt,
            "count":    count,
            "month":    start_dt.month,
        })

    return results


# ── Statistiques saisonnières ─────────────────────────────────────────────────

def _seasonal_stats(historical: list, target_month: int) -> tuple:
    """
    Retourne (moyenne, écart-type) pour le mois cible.
    Fallback sur la moyenne globale si < SX_MIN_SEASONAL fenêtres disponibles.
    """
    seasonal = [h["count"] for h in historical if h["month"] == target_month]

    if len(seasonal) >= SX_MIN_SEASONAL:
        mu = mean(seasonal)
        sd = stdev(seasonal) if len(seasonal) > 1 else max(mu * 0.3, 1.0)
        logger.debug(f"[SX] Stats mois={target_month} (N={len(seasonal)}) μ={mu:.1f} σ={sd:.1f}")
    else:
        all_c = [h["count"] for h in historical]
        if len(all_c) < 2:
            return 0.0, 0.0
        mu = mean(all_c)
        sd = stdev(all_c)
        logger.debug(f"[SX] Stats globales fallback (N={len(all_c)}) μ={mu:.1f} σ={sd:.1f}")

    return mu, max(sd, 1.0)


# ── Bonus pace temps réel ─────────────────────────────────────────────────────

def _pace_multiplier(handle: str, start_str: str, end_dt: datetime,
                     bracket_lower: float, mu: float) -> float:
    """
    Ajuste le zscore si le pace en cours confirme l'improbabilité (≤ 24h restants).
    Retourne :
      0.0  → bracket déjà atteint, signal annulé
      1.0  → pas de bonus (>24h ou pace insuffisant)
      1.2  → bonus (pace confirme l'improbabilité)
    """
    now        = datetime.now(tz=timezone.utc)
    hours_left = (end_dt - now).total_seconds() / 3600
    if hours_left > 24:
        return 1.0

    today = now.strftime("%Y-%m-%d")
    data  = _xget(f"/users/{handle}/posts",
                  {"startDate": start_str[:10], "endDate": today})
    n_current = len(_unwrap(data))
    time.sleep(REQUEST_DELAY)

    remaining_needed = bracket_lower - n_current
    if remaining_needed <= 0:
        logger.debug(f"[SX] {handle} bracket {bracket_lower:.0f} : déjà {n_current} tweets → signal annulé")
        return 0.0

    # Pace historique moyen par heure
    hourly_mu     = mu / (7 * 24)
    expected_left = hourly_mu * hours_left

    if expected_left <= 0:
        return SX_PACE_BONUS

    ratio = remaining_needed / expected_left
    if ratio >= 3.0:
        logger.debug(f"[SX] {handle} pace bonus : {remaining_needed:.0f} tweets nécessaires "
                     f"vs {expected_left:.1f} attendus (ratio={ratio:.1f}×)")
        return SX_PACE_BONUS
    return 1.0


# ── Parsing brackets ──────────────────────────────────────────────────────────

def _parse_bracket(text: str) -> tuple:
    """
    Extrait (borne_inférieure, borne_supérieure) depuis le titre/question d'un bracket.
    Exemples :
      "140-159 tweets" → (140.0, 159.0)
      "160+ tweets"    → (160.0, None)
      "0-19 tweets"    → (0.0, 19.0)
    """
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", text)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"(\d+)\s*\+", text)
    if m:
        return float(m.group(1)), None
    m = re.search(r"(?:less than|fewer than|under|<)\s*(\d+)", text, re.IGNORECASE)
    if m:
        return 0.0, float(m.group(1)) - 1
    return None, None


def _slug_from_link(market_link: str) -> Optional[str]:
    """Extrait le slug d'event depuis une URL polymarket.com/event/..."""
    if not market_link:
        return None
    parts = market_link.rstrip("/").split("/")
    for i, p in enumerate(parts):
        if p == "event" and i + 1 < len(parts):
            return parts[i + 1]
    return None


# ── Fonction principale ───────────────────────────────────────────────────────

def get_sx_candidates(portfolio: dict) -> list:
    """
    Scanne les marchés tweet actifs (xtracker) et retourne les candidats SX.

    Chaque candidat est un dict compatible avec le format signal de live_bot.py :
    {
      "market":          <dict Gamma avec clobTokenIds, outcomePrices, etc.>,
      "strategy":        "SX",
      "win_rate_prior":  <float>,   # estimé depuis zscore
      "reason":          <str>,
      "yes_price":       <float>,
      "ev":              <float>,
      "kelly_mult":      <float>,   # 1/√rank au sein du même marché
    }
    """
    now = datetime.now(tz=timezone.utc)

    # 1. Trackings actifs se terminant dans ≤ 48h
    data      = _xget("/trackings", {"activeOnly": "true"})
    trackings = _unwrap(data)

    active = []
    for t in trackings:
        end_dt = _parse_dt(t.get("endDate", ""))
        if not end_dt:
            continue
        hours_left = (end_dt - now).total_seconds() / 3600
        if 0 < hours_left <= SX_MAX_HOURS:
            t["_end_dt"]     = end_dt
            t["_hours_left"] = hours_left
            active.append(t)

    if not active:
        logger.info("[SX] Aucun marché tweet dans ≤48h")
        return []

    logger.info(f"[SX] {len(active)} marché(s) tweet dans ≤48h — analyse en cours...")

    all_candidates = []
    open_ids       = set(portfolio.get("positions_ouvertes", {}).keys())

    for tracking in active:
        user   = _get_field(tracking, "user", default={})
        if not isinstance(user, dict):
            user = {}
        handle = (
            _get_field(user, "handle", "username", "screen_name")
            or _get_field(tracking, "handle", "username")
        )
        if not handle:
            logger.info(f"[SX] Tracking sans handle — champs disponibles : {list(tracking.keys())}")
            continue

        end_dt    = tracking["_end_dt"]
        start_str = _get_field(tracking, "startDate", "start_date", "startAt")
        market_link = _get_field(tracking, "marketLink", "market_link", "url", "link")
        slug      = _slug_from_link(market_link)

        if not slug:
            logger.info(f"[SX] @{handle} : pas de slug depuis marketLink={market_link!r} "
                        f"— champs tracking : {list(tracking.keys())}")
            continue

        # 2. Historique tweet counts (2 appels : posts + trackings)
        historical = _get_historical_counts(handle)
        if len(historical) < 2:
            logger.info(f"[SX] @{handle} : historique insuffisant ({len(historical)} "
                        f"fenêtres fermées) — xtracker trop récent pour ce compte")
            continue

        # 3. Stats saisonnières (mois de fin du marché)
        mu, sd = _seasonal_stats(historical, end_dt.month)
        if mu == 0:
            continue

        # 4. Sous-marchés (brackets) depuis Gamma
        event_data  = _gamma_get("/events", {"slug": slug})
        events      = _unwrap(event_data) if isinstance(event_data, list) else (
                      [event_data] if isinstance(event_data, dict) else [])
        if not events:
            logger.info(f"[SX] @{handle} : event Gamma introuvable pour slug={slug}")
            continue

        event       = events[0]
        event_id    = str(event.get("id", ""))
        sub_markets = event.get("markets") or []

        # 5. Scorer chaque bracket
        candidates_event = []

        for m in sub_markets:
            if m.get("closed"):
                continue

            mid = str(m.get("id", ""))
            if mid in open_ids:
                continue  # déjà en portefeuille

            question = m.get("question", "") or m.get("title", "") or ""
            lower, _upper = _parse_bracket(question)
            if lower is None or lower == 0:
                continue  # bracket 0-X : pas d'edge NO sur les petits comptes

            # Zscore de base
            zscore = (lower - mu) / sd
            if zscore <= 0:
                continue

            # Bonus pace si ≤ 24h
            pace_mult = _pace_multiplier(handle, start_str, end_dt, lower, mu)
            if pace_mult == 0.0:
                continue  # bracket déjà atteint → signal annulé

            zscore_adj = zscore * pace_mult
            if zscore_adj < SX_ZSCORE_MIN:
                continue

            # Prix YES courant
            raw = m.get("outcomePrices")
            if raw is None:
                continue
            try:
                prices    = json.loads(raw) if isinstance(raw, str) else raw
                yes_price = float(prices[0])
            except (ValueError, IndexError, TypeError):
                continue

            if not (0 < yes_price < 1):
                continue

            # Win rate estimé depuis le zscore (conservateur)
            # A SX_ZSCORE_MIN σ → 95% base, +2pp par σ supplémentaire
            win_rate = min(0.999, 0.95 + (zscore_adj - SX_ZSCORE_MIN) * 0.02)

            # EV normalisé (edge vs probabilité marché)
            no_market_prob = 1.0 - yes_price
            ev             = win_rate - no_market_prob

            m["_event_id"]  = event_id
            m["_sx_handle"] = handle
            m["_sx_zscore"] = zscore_adj
            m["_sx_mu"]     = mu
            m["_sx_sd"]     = sd

            candidates_event.append({
                "market":         m,
                "strategy":       "SX",
                "win_rate_prior": win_rate,
                "reason":         (f"@{handle} bracket {lower:.0f}+ | "
                                   f"z={zscore_adj:.2f} (μ={mu:.1f}, σ={sd:.1f}, "
                                   f"mois={end_dt.month})"),
                "yes_price":      yes_price,
                "ev":             ev,
                "zscore":         zscore_adj,
                "end_dt":         end_dt,
            })

        # 6. Trier par zscore décroissant et assigner kelly_mult = 1/√rank
        candidates_event.sort(key=lambda x: x["zscore"], reverse=True)
        for rank, cand in enumerate(candidates_event, start=1):
            cand["kelly_mult"] = round(1.0 / math.sqrt(rank), 4)
            logger.info(
                f"  [SX] @{handle} | bracket z={cand['zscore']:.2f} | "
                f"YES={cand['yes_price']:.3f} | Kelly×{cand['kelly_mult']:.2f} | "
                f"{cand['market'].get('question','')[:50]}"
            )

        all_candidates.extend(candidates_event)
        time.sleep(REQUEST_DELAY)

    logger.info(f"[SX] {len(all_candidates)} candidat(s) SX trouvé(s)")
    return all_candidates
