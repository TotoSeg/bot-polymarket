"""
polymarket_common/gamma.py
============================
Appels Gamma API partagés (lecture publique, sans authentification).

Le bot directionnel a sa propre couche Gamma dans
src/phase5_paper/polymarket_client.py (non modifiée ici, cf. étape 1 —
ne pas toucher à bot_directional avant l'étape 2). Ce module est générique
et sera utilisé principalement par bot_lp/scanner.py.
"""

import requests
from datetime import datetime, timezone
from typing import Optional

GAMMA_BASE = "https://gamma-api.polymarket.com"

CATEGORY_TAG_IDS = {
    "geopolitics": 100265,
    "politics":    2,
    "finance":     120,
    "crypto":      21,
    "sports":      100639,
    "tech":        1401,
    "culture":     596,
}


def get_active_markets(
    tag_id: Optional[int] = None,
    order_by: str = "rewardEpoch",
    limit: int = 100,
) -> list[dict]:
    """Retourne les marchés actifs, triés par ordre décroissant de rewardEpoch par défaut."""
    params = {
        "active":    "true",
        "closed":    "false",
        "order":     order_by,
        "ascending": "false",
        "limit":     limit,
    }
    if tag_id:
        params["tag_id"] = tag_id
        params["related_tags"] = "true"

    resp = requests.get(f"{GAMMA_BASE}/markets", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_market(condition_id: str) -> dict:
    """Retourne le détail d'un marché par condition_id."""
    resp = requests.get(f"{GAMMA_BASE}/markets/{condition_id}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def days_to_end(market: dict) -> float:
    """Calcule le nombre de jours restants avant résolution."""
    end = datetime.fromisoformat(market["endDate"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return (end - now).total_seconds() / 86400


def is_fee_free(market: dict) -> bool:
    """True si le marché est en catégorie fee-free (géopolitique/world events)."""
    return not market.get("feesEnabled", False)
