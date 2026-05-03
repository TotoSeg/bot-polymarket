"""
Phase 6 — Exécution des ordres réels sur Polymarket CLOB V2
============================================================
Utilise py-clob-client-v2 (CLOB V2 lancé le 28 avril 2026).

Changements V1 → V2 :
  - Package : py-clob-client-v2
  - Méthode unifiée : create_and_post_market_order() (create + post en un seul appel)
  - Side enum : Side.BUY au lieu de la string "BUY"
  - PartialCreateOrderOptions(tick_size="0.01") obligatoire
  - EIP-712 domain version "2" gérée automatiquement par le SDK
"""

import os, json
from typing import Optional
from loguru import logger

from py_clob_client_v2 import (
    ClobClient,
    ApiCreds,
    MarketOrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)


# ── Connexion au CLOB ─────────────────────────────────────────────────────────

def build_client() -> ClobClient:
    """
    Instancie le client CLOB V2 authentifié depuis les variables d'env.
    signature_type=1 (POLY_PROXY) + funder = adresse proxy pUSD obligatoires
    pour que le CLOB identifie correctement le solde du wallet proxy.
    """
    pk     = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.environ.get("POLYMARKET_PROXY_WALLET", "").strip() or None
    creds  = ApiCreds(
        api_key        = os.environ["POLYMARKET_API_KEY"],
        api_secret     = os.environ["POLYMARKET_API_SECRET"],
        api_passphrase = os.environ["POLYMARKET_API_PASSPHRASE"],
    )
    return ClobClient(
        host           = "https://clob.polymarket.com",
        chain_id       = 137,
        key            = pk,
        creds          = creds,
        signature_type = 1,
        funder         = funder,
    )


def create_api_keys(private_key: str) -> dict:
    """Génère les clés API L2 depuis la clé privée. À appeler une seule fois."""
    client = ClobClient(
        host     = "https://clob.polymarket.com",
        chain_id = 137,
        key      = private_key,
    )
    creds = client.create_or_derive_api_creds()
    return {
        "api_key":        creds.api_key,
        "api_secret":     creds.api_secret,
        "api_passphrase": creds.passphrase,
    }


# ── Helpers token ──────────────────────────────────────────────────────────────

def get_no_token_id(market: dict) -> Optional[str]:
    """
    Extrait le token_id NO depuis la réponse Gamma API.
    clobTokenIds : ["<yes_token_id>", "<no_token_id>"]
    """
    clob_ids = market.get("clobTokenIds")
    outcomes = market.get("outcomes")

    if not clob_ids:
        return None

    if isinstance(clob_ids, str):
        try:
            clob_ids = json.loads(clob_ids)
        except (json.JSONDecodeError, ValueError):
            return None

    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (json.JSONDecodeError, ValueError):
            outcomes = None

    if outcomes and isinstance(outcomes, list):
        for i, o in enumerate(outcomes):
            if str(o).lower() in ("no", "non"):
                return str(clob_ids[i]) if i < len(clob_ids) else None

    return str(clob_ids[1]) if len(clob_ids) >= 2 else None


# ── Vérification de liquidité ─────────────────────────────────────────────────

def check_liquidity(client: ClobClient, token_id: str, amount_usdc: float) -> bool:
    """Vérifie qu'au moins 50% de la mise est couverte par le carnet d'ordres."""
    try:
        book = client.get_order_book(token_id)
        # V2 retourne un dict ou un objet selon la version
        if isinstance(book, dict):
            asks = book.get("asks") or []
        else:
            asks = book.asks or []
        total = sum(float(a["size"] if isinstance(a, dict) else a.size) *
                    float(a["price"] if isinstance(a, dict) else a.price)
                    for a in asks)
        if total < amount_usdc * 0.5:
            logger.warning(f"Liquidité faible : {total:.1f}$ dispo pour {amount_usdc}$ demandés")
            return False
        return True
    except Exception as e:
        logger.warning(f"Impossible de lire l'order book : {e}")
        return True


# ── Placement d'ordre (CLOB V2) ───────────────────────────────────────────────

def place_no_order(client: ClobClient, token_id: str,
                   amount_usdc: float, yes_price: float) -> Optional[dict]:
    """
    Achète amount_usdc de tokens NO via CLOB V2.
    tick_size="0.01" couvre la quasi-totalité des marchés Polymarket.
    """
    try:
        resp = client.create_and_post_market_order(
            order_args = MarketOrderArgs(
                token_id   = token_id,
                amount     = amount_usdc,
                side       = Side.BUY,
                order_type = OrderType.FOK,
            ),
            options    = PartialCreateOrderOptions(tick_size="0.01"),
            order_type = OrderType.FOK,
        )
        no_price = round(1.0 - yes_price, 4)
        logger.success(
            f"  Ordre NO placé : token={token_id[:15]}... | "
            f"{amount_usdc}$ @ NO={no_price:.3f} | resp={resp}"
        )
        return resp
    except Exception as e:
        logger.error(f"  Ordre échoué (token={token_id[:15]}...) : {e}")
        return None


# ── Lecture du solde ──────────────────────────────────────────────────────────

def get_usdc_balance(client: ClobClient) -> float:
    """Retourne le solde pUSD disponible sur le compte Polymarket."""
    try:
        bal = client.get_balance_allowance(params={"asset_type": "COLLATERAL"})
        return float(bal.get("balance", 0)) / 1e6
    except Exception as e:
        logger.warning(f"Solde non récupéré : {e}")
        return 0.0
