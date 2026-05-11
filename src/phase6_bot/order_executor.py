"""
Phase 6 – Exécution des ordres réels sur Polymarket CLOB V2
===========================================================
Utilise py-clob-client-v2 (CLOB V2 lancé le 28 avril 2026).

Changements V1 → V2 :
  - Package : py-clob-client-v2
  - Méthode unifiée : create_and_post_market_order() (create + post en un seul appel)
  - Side enum : Side.BUY / Side.SELL au lieu de la string "BUY"/"SELL"
  - PartialCreateOrderOptions(tick_size="0.01") obligatoire
  - EIP-712 domain version "2" générée automatiquement par le SDK
"""

import os, json
import requests
from typing import Optional
from loguru import logger

from py_clob_client_v2 import (
    ClobClient,
    ApiCreds,
    MarketOrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
    AssetType,
    BalanceAllowanceParams,
)


# ── Connexion au CLOB ────────────────────────────────────────────────────────

def build_client() -> ClobClient:
    """
    Instancie le client CLOB V2 authentifié depuis les variables d'env.
    signature_type=2 (GNOSIS_SAFE) + funder = adresse proxy pUSD obligatoires
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
        signature_type = 2,       # GNOSIS_SAFE – MetaMask proxy Polymarket
        funder         = funder,
    )


def create_api_keys(private_key: str) -> dict:
    """Génère les clés API L2 depuis la clé privée. À appeler une seule fois."""
    client = ClobClient(
        host      = "https://clob.polymarket.com",
        chain_id  = 137,
        key       = private_key,
    )
    creds = client.create_or_derive_api_creds()
    return {
        "api_key":        creds.api_key,
        "api_secret":     creds.api_secret,
        "api_passphrase": creds.passphrase,
    }


# ── Helpers token ────────────────────────────────────────────────────────────

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


# ── Vérification de liquidité (achat) ───────────────────────────────────────

def check_liquidity(client: ClobClient, token_id: str, amount_usdc: float) -> bool:
    """Vérifie qu'au moins 50% de la mise est couverte par le carnet d'ordres (côté achat NO)."""
    try:
        book = client.get_order_book(token_id)
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
        return True   # en cas d'erreur API, on tente quand même


# ── Vérification de liquidité (vente) ───────────────────────────────────────

def check_sell_liquidity(client: ClobClient, token_id: str,
                         amount_tokens: float, min_price: float) -> bool:
    """
    Vérifie qu'on peut vendre nos tokens NO sans P&L négatif.

    Lit les offres d'achat (bids) dans le carnet pour le token NO.
    Vérifie que la valeur totale des bids à un prix ≥ min_price couvre
    au moins 50% de la valeur de la vente souhaitée.

    Args:
        token_id      : token_id du NO
        amount_tokens : nombre de tokens NO à vendre
        min_price     : prix minimum acceptable (breakeven incluant frais)
    """
    try:
        book = client.get_order_book(token_id)
        if isinstance(book, dict):
            bids = book.get("bids") or []
        else:
            bids = book.bids or []

        # Sommer la valeur des bids au prix >= min_price
        total_value = 0.0
        for b in bids:
            price = float(b["price"] if isinstance(b, dict) else b.price)
            size  = float(b["size"]  if isinstance(b, dict) else b.size)
            if price >= min_price:
                total_value += size * price

        target_value = amount_tokens * min_price
        if target_value <= 0:
            return True   # min_price=0 → accepte n'importe quel prix (cleanup)

        if total_value < target_value * 0.5:
            logger.warning(f"Liquidité vente insuffisante : {total_value:.2f}$ de bids "
                           f"@ prix≥{min_price:.3f} pour {target_value:.2f}$ souhaités")
            return False
        return True
    except Exception as e:
        logger.warning(f"Impossible de lire l'order book (vente) : {e}")
        return False   # par prudence, ne pas vendre si on ne peut pas vérifier


# ── Placement d'ordre achat (CLOB V2) ────────────────────────────────────────

def place_no_order(client: ClobClient, token_id: str,
                   amount_usdc: float, yes_price: float) -> Optional[dict]:
    """
    Achète amount_usdc de tokens NO via CLOB V2.
    tick_size="0.01" couvre la quasi-totalité des marchés Polymarket.
    """
    no_price     = min(round(1.0 - yes_price, 4), 0.99)  # CLOB max price = 0.99
    amount_usdc  = max(round(amount_usdc, 2), 1.0)        # CLOB min order = $1

    try:
        resp = client.create_and_post_market_order(
            order_args  = MarketOrderArgs(
                token_id   = token_id,
                amount     = amount_usdc,
                side       = Side.BUY,
                order_type = OrderType.FOK,
            ),
            options     = PartialCreateOrderOptions(tick_size="0.01"),
            order_type  = OrderType.FOK,
        )
        logger.success(
            f"  Ordre NO placé : token={token_id[:15]}...  "
            f"{amount_usdc}$ @ NO={no_price:.3f} | resp={resp}"
        )
        return resp
    except Exception as e:
        logger.error(f"  Ordre échoué (token={token_id[:15]}...) : {e}")
        return None


# ── Placement d'ordre vente (CLOB V2) ────────────────────────────────────────

def sell_no_position(client: ClobClient, token_id: str,
                     amount_tokens: float, min_price: float) -> Optional[dict]:
    """
    Vend amount_tokens de tokens NO via CLOB V2.

    Pour les ordres de VENTE, 'amount' est le nombre de tokens à vendre
    (et non des USDC). min_price est fourni à titre informatif dans les logs ;
    l'ordre FOK sera exécuté au meilleur bid disponible.

    Args:
        token_id      : token_id du token NO
        amount_tokens : nombre de tokens NO à vendre
        min_price     : prix minimum souhaité (non garanti en FOK, juste loggé)
    """
    amount_tokens = max(round(amount_tokens, 6), 0.000001)

    try:
        resp = client.create_and_post_market_order(
            order_args  = MarketOrderArgs(
                token_id   = token_id,
                amount     = amount_tokens,
                side       = Side.SELL,
                order_type = OrderType.FOK,
            ),
            options     = PartialCreateOrderOptions(tick_size="0.01"),
            order_type  = OrderType.FOK,
        )
        logger.success(
            f"  Vente NO : token={token_id[:15]}...  "
            f"{amount_tokens:.4f} tokens (prix min souhaité {min_price:.3f}) | resp={resp}"
        )
        return resp
    except Exception as e:
        logger.error(f"  Vente échouée (token={token_id[:15]}...) : {e}")
        return None


# ── Positions ouvertes sur Polymarket ────────────────────────────────────────

def get_all_clob_positions() -> list[dict]:
    """
    Récupère toutes les positions ouvertes du wallet depuis l'API CLOB.
    Utilise l'adresse proxy (POLYMARKET_PROXY_WALLET) du .env.

    Retourne une liste de dicts avec au minimum :
        asset_id  : token_id du token détenu (YES ou NO)
        balance   : nombre de tokens (float)
        outcome   : "YES" ou "NO"
        market    : market_id associé (si disponible)
    """
    funder = os.environ.get("POLYMARKET_PROXY_WALLET", "").strip()
    if not funder:
        logger.warning("POLYMARKET_PROXY_WALLET absent du .env — impossible de lister les positions CLOB")
        return []
    try:
        resp = requests.get(
            "https://clob.polymarket.com/data/position",
            params={"user": funder, "sizeThreshold": "0.001"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
        # Certaines versions retournent {"positions": [...]}
        if isinstance(data, dict):
            return data.get("positions", [])
        return []
    except Exception as e:
        logger.warning(f"Erreur récupération positions CLOB : {e}")
        return []


# ── Lecture du solde ─────────────────────────────────────────────────────────

def get_usdc_balance(client: ClobClient) -> float:
    """Retourne le solde pUSD disponible sur le compte Polymarket."""
    try:
        bal = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return float(bal.get("balance", 0)) / 1e6
    except Exception as e:
        logger.warning(f"Solde non récupéré : {e}")
        return 0.0
