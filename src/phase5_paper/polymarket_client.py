"""
Phase 5 – Client API Polymarket
===============================
Accès aux données en temps réel via l'API Gamma (métadonnées + prix).

Endpoints utilisés :
  GET https://gamma-api.polymarket.com/markets
    ➔ Liste des marchés actifs avec prix YES/NO courants
  GET https://gamma-api.polymarket.com/events
    ➔ Liste des événements (contient les marchés neg-risk groupés)
  GET https://gamma-api.polymarket.com/markets/{id}
    ➔ Détail d'un marché (résolution, prix finaux)
  GET https://gamma-api.polymarket.com/markets?clobTokenIds={token_id}
    ➔ Marché associé à un token_id (lookup inverse)

Format des prix (outcomeePrices) :
  Marché ouvert  : ["0.97", "0.03"]  ➔ YES 97%, NO 3%
  Résolu YES     : ["1", "0"]         ➔ YES a gagné
  Résolu NO      : ["0", "1"]         ➔ NO a gagné

Usage :
    from src.phase5_paper.polymarket_client import get_active_markets, get_market
"""

import sys
import json
import time
from typing import Optional

import requests
from loguru import logger

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GAMMA_API     = "https://gamma-api.polymarket.com"
PAGE_SIZE     = 100
REQUEST_DELAY = 0.15


def get_active_markets(min_volume: float = 500.0, max_pages: int = 30) -> list[dict]:
    """
    Récupère tous les marchés OUVERTS depuis l'API Gamma.

    min_volume : volume minimum en USD
    max_pages  : limite de pages (sécurité anti-boucle infinie)
    """
    all_markets = []
    offset = 0

    for page in range(max_pages):
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={"closed": "false", "limit": PAGE_SIZE, "offset": offset},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.warning(f"Erreur API page {page} : {e}")
            break

        if not data:
            break

        for m in data:
            vol = float(m.get("volume", 0) or 0)
            if vol >= min_volume:
                all_markets.append(m)

        logger.debug(f"  Page {page+1} : {len(data)} marchés récupérés (total : {len(all_markets)})")

        if len(data) < PAGE_SIZE:
            break

        offset += PAGE_SIZE
        time.sleep(REQUEST_DELAY)

    logger.info(f"Marchés actifs récupérés (vol >= {min_volume}$) : {len(all_markets)}")
    return all_markets


def get_active_event_markets(min_volume: float = 500.0, max_pages: int = 20) -> list[dict]:
    """
    Récupère les marchés contenus dans les événements Gamma (endpoint /events).

    Les marchés neg-risk groupés (ex: "by May 26?", "by May 27?") n'apparaissent
    pas dans /markets mais sont accessibles via /events → chaque event contient
    un tableau 'markets' avec les sous-marchés individuels.

    Les événements "restricted" (ex: Iran ceasefire) n'apparaissent pas dans la
    pagination standard → on les fetch directement par slug via PRIORITY_SLUGS.

    Retourne une liste plate de marchés, dans le même format que get_active_markets().
    """
    # Événements à toujours inclure car absents de la pagination standard (restricted=true)
    PRIORITY_SLUGS = [
        "iran-ceasefire-continues-through",
    ]

    all_markets = []
    seen_ids    = set()

    def _add_sub_markets(event):
        event_id = str(event.get("id", "")).strip()
        for m in (event.get("markets") or []):
            if m.get("closed"):
                continue
            vol = float(m.get("volume", 0) or 0)
            if vol < min_volume:
                continue
            mid = str(m.get("id", ""))
            if mid and mid not in seen_ids:
                # Injecter l'ID de l'événement parent pour bloquer les
                # positions croisées (SY sur A + S3 sur B du même event)
                m["_event_id"] = event_id
                all_markets.append(m)
                seen_ids.add(mid)

    # ── Fetch prioritaire par slug ────────────────────────────────────────────
    for slug in PRIORITY_SLUGS:
        try:
            resp = requests.get(
                f"{GAMMA_API}/events",
                params={"slug": slug},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                for event in data:
                    _add_sub_markets(event)
            time.sleep(REQUEST_DELAY)
        except requests.RequestException as e:
            logger.warning(f"Erreur fetch slug {slug} : {e}")

    # ── Pagination standard ───────────────────────────────────────────────────
    offset = 0
    for page in range(max_pages):
        try:
            resp = requests.get(
                f"{GAMMA_API}/events",
                params={"closed": "false", "limit": PAGE_SIZE, "offset": offset},
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
        except requests.RequestException as e:
            logger.warning(f"Erreur /events page {page} : {e}")
            break

        if not events:
            break

        for event in events:
            _add_sub_markets(event)

        if len(events) < PAGE_SIZE:
            break

        offset += PAGE_SIZE
        time.sleep(REQUEST_DELAY)

    # ── Pagination restricted=true (élections, événements spéciaux) ──────────
    # Ces événements n'apparaissent PAS dans la pagination standard.
    # Exemples : São Tomé presidential election, Iran ceasefire, etc.
    offset = 0
    for page in range(max_pages):
        try:
            resp = requests.get(
                f"{GAMMA_API}/events",
                params={"closed": "false", "restricted": "true",
                        "limit": PAGE_SIZE, "offset": offset},
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
        except requests.RequestException as e:
            logger.warning(f"Erreur /events restricted page {page} : {e}")
            break

        if not events:
            break

        for event in events:
            _add_sub_markets(event)

        if len(events) < PAGE_SIZE:
            break

        offset += PAGE_SIZE
        time.sleep(REQUEST_DELAY)

    logger.info(f"Marchés via /events récupérés (vol >= {min_volume}$) : {len(all_markets)}")
    return all_markets


def get_market(market_id: str) -> Optional[dict]:
    """Récupère le détail d'un marché par son ID."""
    try:
        resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.warning(f"Erreur get_market({market_id}) : {e}")
        return None


def get_market_by_token_id(token_id: str) -> Optional[dict]:
    """
    Récupère le marché associé à un token_id (YES ou NO).
    Utile pour retrouver un marché à partir d'une position CLOB.
    """
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"clobTokenIds": token_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return None
    except requests.RequestException as e:
        logger.warning(f"Erreur get_market_by_token_id({token_id[:12]}...) : {e}")
        return None


def parse_yes_price(market: dict) -> Optional[float]:
    """Extrait le prix YES courant d'un marché."""
    raw = market.get("outcomePrices")
    if raw is None:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        return float(prices[0])
    except (ValueError, IndexError, TypeError):
        return None


def parse_resolution(market: dict) -> Optional[int]:
    """
    Extrait le résultat d'un marché résolu.
    Retourne 0 (NO gagne), 1 (YES gagne), ou None (non résolu).
    """
    if not market.get("closed", False):
        return None

    raw = market.get("outcomePrices")
    if raw is None:
        return None

    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        first = float(prices[0])
        if first == 1.0:
            return 1
        elif first == 0.0:
            return 0
        return None
    except (ValueError, IndexError, TypeError):
        return None
