"""
Phase 5 — Client API Polymarket
================================
Accès aux données en temps réel via l'API Gamma (métadonnées + prix).

Endpoints utilisés :
  GET https://gamma-api.polymarket.com/markets
    → Liste des marchés actifs avec prix YES/NO courants
  GET https://gamma-api.polymarket.com/markets/{id}
    → Détail d'un marché (résolution, prix finaux)

Format des prix (outcomePrices) :
  Marché ouvert  : ["0.97", "0.03"]   → YES 97%, NO 3%
  Résolu YES     : ["1", "0"]         → YES a gagné
  Résolu NO      : ["0", "1"]         → NO a gagné

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

GAMMA_API   = "https://gamma-api.polymarket.com"
PAGE_SIZE   = 100     # Marchés par requête
REQUEST_DELAY = 0.15  # Secondes entre requêtes (éviter ban)


def get_active_markets(min_volume: float = 500.0, max_pages: int = 30) -> list[dict]:
    """
    Récupère tous les marchés OUVERTS (non résolus) depuis l'API Gamma.

    min_volume : volume minimum en USD pour filtrer les marchés trop petits
    max_pages  : limite de pages à charger (sécurité anti-boucle infinie)

    Retourne une liste de dicts avec au minimum :
        id, question, outcomePrices, volume, endDate, createdAt, closed
    """
    all_markets = []
    offset = 0

    for page in range(max_pages):
        try:
            resp = requests.get(
                f"{GAMMA_API}/markets",
                params={"closed": "false", "active": "true", "limit": PAGE_SIZE, "offset": offset},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.warning(f"Erreur API page {page} : {e}")
            break

        if not data:
            break

        # Filtrer sur le volume minimum
        for m in data:
            vol = float(m.get("volume", 0) or 0)
            if vol >= min_volume:
                all_markets.append(m)

        logger.debug(f"  Page {page+1} : {len(data)} marchés récupérés (total : {len(all_markets)})")

        if len(data) < PAGE_SIZE:
            break  # Dernière page

        offset += PAGE_SIZE
        time.sleep(REQUEST_DELAY)

    logger.info(f"Marchés actifs récupérés (vol >= {min_volume}$) : {len(all_markets)}")
    return all_markets


def get_market(market_id: str) -> Optional[dict]:
    """
    Récupère le détail d'un marché spécifique (pour vérifier sa résolution).

    Retourne None si le marché n'existe pas ou en cas d'erreur réseau.
    """
    try:
        resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.warning(f"Erreur get_market({market_id}) : {e}")
        return None


def parse_yes_price(market: dict) -> Optional[float]:
    """
    Extrait le prix YES courant d'un marché.

    outcomePrices peut être une string JSON ou une liste Python.
    Retourne None si le parsing échoue.
    """
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

    Retourne :
        0  si NO a gagné (outcomePrices[0] == "0")
        1  si YES a gagné (outcomePrices[0] == "1")
        None si le marché n'est pas encore résolu
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
            return 1  # YES a gagné
        elif first == 0.0:
            return 0  # NO a gagné
        return None  # Pas encore tranché
    except (ValueError, IndexError, TypeError):
        return None
