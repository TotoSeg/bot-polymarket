"""
Phase 6 — Exécution des ordres réels sur Polymarket CLOB
==========================================================
Wraps le py-clob-client pour placer des ordres NO (stratégie S3).

Flux complet pour un marché S3 :
  1. signal détecté sur Gamma API (YES entre 5-10%)
  2. get_no_token_id()    → récupère le token_id du côté NO
  3. check_liquidity()    → vérifie qu'on peut acheter la quantité voulue
  4. place_no_order()     → envoie l'ordre FOK (Fill-or-Kill)
  5. retourne l'order_id  → suivi dans live_portfolio.json

Gestion des erreurs :
  - token_id absent       → skip marché, log warning
  - liquidité insuffisante → skip, log warning
  - ordre rejeté           → skip, log error (pas de retry automatique)
"""

import os, json, time
from typing import Optional
from loguru import logger

from py_clob_client.client     import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, AssetType, BalanceAllowanceParams


# ── Connexion au CLOB ─────────────────────────────────────────────────────────

def build_client() -> ClobClient:
    """
    Instancie le client CLOB authentifié depuis les variables d'env.
    Requiert POLYMARKET_PRIVATE_KEY + les 3 clés API dans .env.
    """
    from py_clob_client.clob_types import ApiCreds
    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    return ClobClient(
        host     = "https://clob.polymarket.com",
        key      = pk,
        chain_id = 137,   # Polygon
        creds    = ApiCreds(
            api_key        = os.environ["POLYMARKET_API_KEY"],
            api_secret     = os.environ["POLYMARKET_API_SECRET"],
            api_passphrase = os.environ["POLYMARKET_API_PASSPHRASE"],
        ),
    )


def create_api_keys(private_key: str) -> dict:
    """
    Génère les clés API L2 Polymarket à partir de la clé privée du wallet.
    À appeler une fois lors de la configuration initiale.
    Retourne {"api_key", "api_secret", "api_passphrase"}.
    """
    client = ClobClient(
        host     = "https://clob.polymarket.com",
        key      = private_key,
        chain_id = 137,
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
    Extrait le token_id du côté NO depuis la réponse de l'API Gamma.

    Gamma API retourne :
      clobTokenIds : ["<yes_token_id>", "<no_token_id>"]
      outcomes     : ["Yes", "No"]

    On cherche l'index de "No" dans outcomes pour récupérer le bon token.
    Fallback : index 1 si le format est binaire standard.
    """
    clob_ids = market.get("clobTokenIds")
    outcomes = market.get("outcomes")

    if not clob_ids:
        return None

    # Tenter de parser si c'est une chaîne JSON
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

    # Trouver l'index de "No"
    if outcomes and isinstance(outcomes, list):
        for i, o in enumerate(outcomes):
            if str(o).lower() in ("no", "non"):
                return str(clob_ids[i]) if i < len(clob_ids) else None

    # Fallback binaire : index 1 = NO
    return str(clob_ids[1]) if len(clob_ids) >= 2 else None


# ── Vérification de liquidité ─────────────────────────────────────────────────

def check_liquidity(client: ClobClient, token_id: str, amount_usdc: float) -> bool:
    """
    Vérifie qu'on peut acheter `amount_usdc` de tokens NO au marché.
    Regarde les asks dans le carnet d'ordres et calcule la profondeur disponible.
    Retourne True si la liquidité est suffisante.
    """
    try:
        book  = client.get_order_book(token_id)
        asks  = book.asks or []
        total = sum(float(a.size) * float(a.price) for a in asks)
        if total < amount_usdc * 0.5:   # accepter si ≥ 50% de la mise est couverte
            logger.warning(f"Liquidité faible : {total:.1f}$ disponible pour {amount_usdc}$ demandés")
            return False
        return True
    except Exception as e:
        logger.warning(f"Impossible de lire l'order book : {e}")
        return True  # laisser passer, l'ordre FOK échouera proprement si pas de liquidité


# ── Placement d'ordre ─────────────────────────────────────────────────────────

def place_no_order(client: ClobClient, token_id: str,
                   amount_usdc: float, yes_price: float) -> Optional[dict]:
    """
    Achète `amount_usdc` de tokens NO sur le marché.

    Pour S3 : YES est à 5-10%, donc NO est à 90-95%.
    On achète NO → on gagne si l'événement ne se réalise pas (scénario 98%+ des cas).

    Paramètres :
      token_id   : identifiant du token NO (depuis clobTokenIds[1])
      amount_usdc: montant à miser en USDC
      yes_price  : prix YES actuel (0.05-0.10) — utilisé pour logger le contexte

    Retourne le dict de réponse CLOB avec order_id, ou None si échec.
    """
    no_price = round(1.0 - yes_price, 4)

    try:
        order = client.create_market_order(
            MarketOrderArgs(
                token_id   = token_id,
                amount     = amount_usdc,
                side       = "BUY",          # on achète le token NO
                price      = no_price,       # prix indicatif pour le FOK
                order_type = OrderType.FOK,  # Fill-or-Kill : exécuté entièrement ou annulé
            )
        )
        resp = client.post_order(order, OrderType.FOK)
        logger.success(f"  Ordre NO placé : token={token_id[:15]}... | "
                       f"{amount_usdc}$ @ NO={no_price:.3f} | resp={resp}")
        return resp
    except Exception as e:
        logger.error(f"  Ordre échoué (token={token_id[:15]}...) : {e}")
        return None


# ── Lecture du solde ──────────────────────────────────────────────────────────

def get_usdc_balance(client: ClobClient) -> float:
    """Retourne le solde USDC disponible sur le compte Polymarket."""
    try:
        bal = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.USDC)
        )
        return float(bal.get("balance", 0)) / 1e6   # USDC a 6 décimales
    except Exception as e:
        logger.warning(f"Solde USDC non récupéré : {e}")
        return 0.0
