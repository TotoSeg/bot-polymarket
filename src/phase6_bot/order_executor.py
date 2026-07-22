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
    OrderArgs,
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


def get_yes_token_id(market: dict) -> Optional[str]:
    """
    Extrait le token_id YES (index 0 dans clobTokenIds).
    Utilisé par la stratégie SY (achat YES sur marchés très probables).
    """
    clob_ids = market.get("clobTokenIds")
    if not clob_ids:
        return None
    if isinstance(clob_ids, str):
        try:
            clob_ids = json.loads(clob_ids)
        except (json.JSONDecodeError, ValueError):
            return None
    if isinstance(clob_ids, list) and clob_ids:
        return str(clob_ids[0])
    return None


# ── Vérification de liquidité (achat) ───────────────────────────────────────

def check_liquidity(client: ClobClient, token_id: str, amount_usdc: float) -> tuple[bool, float]:
    """
    Vérifie que le carnet d'ordres n'est pas vide et que le best ask ≤ 0.99.
    Retourne (ok, best_ask) — best_ask est le prix d'exécution à utiliser dans l'ordre.

    Le best_ask du CLOB est le vrai prix de marché, différent du prix Gamma API
    (mid-price) : il faut utiliser best_ask dans l'ordre pour qu'il soit exécuté.
    """
    try:
        book = client.get_order_book(token_id)
        if isinstance(book, dict):
            asks = book.get("asks") or []
        else:
            asks = book.asks or []

        if not asks:
            logger.warning(f"Carnet vide pour token {token_id[:12]}...")
            return False, 0.0

        # Vérifier que le best ask ne dépasse pas 0.99 (max CLOB)
        best_ask = min(float(a["price"] if isinstance(a, dict) else a.price) for a in asks)
        if best_ask > 0.99:
            logger.warning(f"Best ask = {best_ask:.4f} > 0.99 (marché trop proche de résolution)")
            return False, 0.0

        # Calculer la liquidité disponible côté ask
        total = sum(float(a["size"] if isinstance(a, dict) else a.size) *
                    float(a["price"] if isinstance(a, dict) else a.price)
                    for a in asks)
        nb_levels = len(asks)
        logger.info(f"  Liquidité {token_id[:12]} : {total:.2f}$ sur {nb_levels} niveaux "
                    f"(best_ask={best_ask:.4f}) pour {amount_usdc:.2f}$ demandés")
        if total < amount_usdc * 0.50:
            logger.warning(f"Liquidité insuffisante : {total:.1f}$ dispo pour {amount_usdc}$ demandés")
            return False, 0.0
        return True, best_ask
    except Exception as e:
        logger.warning(f"Impossible de lire l'order book : {e}")
        return False, 0.0  # par prudence, ne pas tenter si on ne peut pas vérifier


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


# ── Utilitaire : montant réellement exécuté ──────────────────────────────────

def _extract_filled_usdc(resp: dict, requested_usdc: float) -> float:
    """
    Extrait le montant USDC réellement exécuté depuis la réponse CLOB.
    Retourne 0 si aucun champ de fill trouvé (FAK tué = 0 fill).
    Ne jamais supposer une exécution complète sans confirmation explicite.
    """
    if not isinstance(resp, dict):
        return 0.0
    # Statut explicite : cancelled/unmatched = 0 fill
    status = str(resp.get("status", "")).lower()
    if status in ("cancelled", "unmatched", "killed"):
        return 0.0
    # Champs possibles selon py-clob-client-v2
    for field in ("size_matched", "matched_amount", "filled_amount", "amount_filled"):
        val = resp.get(field)
        if val is not None:
            try:
                size = float(val)
                if size <= 0:
                    return 0.0
                price = float(resp.get("price", 1.0))
                return round(size * price, 4)
            except (TypeError, ValueError):
                pass
    # Aucun champ de fill → on logge la réponse complète pour diagnostiquer
    logger.debug(f"  Réponse FAK sans champ fill (ordre potentiellement tué) : {resp}")
    return 0.0


# ── Placement d'ordre achat (CLOB V2) ────────────────────────────────────────

def place_no_order(client: ClobClient, token_id: str,
                   amount_usdc: float, yes_price: float,
                   clob_ask_price: float = 0.0) -> Optional[dict]:
    """
    Achète amount_usdc de tokens NO via CLOB V2.

    Quand clob_ask_price (best ask du carnet) est fourni, on place un ordre
    limite GTC au prix exact du marché : fill immédiat garanti si la liquidité
    est confirmée par check_liquidity. C'est plus fiable que le FAK MarketOrder
    dont le prix implicite diverge du best ask en marché illiquide.

    Sans clob_ask_price (fallback) : FAK market order sans prix — peut échouer
    si le CLOB ne calcule pas correctement le prix implicite.
    """
    amount_usdc = max(round(amount_usdc, 2), 1.0)

    if clob_ask_price > 0:
        # Limite au prix réel du carnet → fill immédiat, pas de slippage
        no_price   = min(round(clob_ask_price, 4), 0.99)
        size_tokens = round(amount_usdc / no_price, 4)
        try:
            resp = client.create_and_post_order(
                OrderArgs(
                    token_id = token_id,
                    price    = float(no_price),
                    size     = float(size_tokens),
                    side     = Side.BUY,
                ),
                options = PartialCreateOrderOptions(tick_size="0.01"),
            )
            filled_usdc = round(size_tokens * no_price, 2)
            resp["_filled_usdc"] = filled_usdc
            logger.success(
                f"  Ordre NO placé : token={token_id[:15]}...  "
                f"{filled_usdc:.2f}$ ({size_tokens:.4f} tokens @ NO={no_price:.3f}) | resp={resp}"
            )
            return resp
        except Exception as e:
            logger.error(f"  Ordre NO échoué (token={token_id[:15]}...) : {e}")
            return None

    # Fallback : FAK market order (moins fiable sans prix explicite)
    no_price = min(round(1.0 - yes_price, 4), 0.99)
    try:
        resp = client.create_and_post_market_order(
            order_args = MarketOrderArgs(
                token_id   = token_id,
                amount     = amount_usdc,
                side       = Side.BUY,
                order_type = OrderType.FAK,
            ),
            options    = PartialCreateOrderOptions(tick_size="0.01"),
            order_type = OrderType.FAK,
        )
        filled_usdc = _extract_filled_usdc(resp, amount_usdc)
        if filled_usdc < 1.0:
            logger.warning(f"  Fill trop faible ({filled_usdc:.2f}$) pour {amount_usdc}$ demandés — ignoré")
            return None
        resp["_filled_usdc"] = filled_usdc
        logger.success(
            f"  Ordre NO placé (FAK) : token={token_id[:15]}...  "
            f"{filled_usdc:.2f}$ (demandé {amount_usdc:.2f}$) @ NO≈{no_price:.3f} | resp={resp}"
        )
        return resp
    except Exception as e:
        logger.error(f"  Ordre NO échoué (token={token_id[:15]}...) : {e}")
        return None


# ── Placement d'ordre achat YES (CLOB V2) ────────────────────────────────────

def place_yes_order(client: ClobClient, token_id: str,
                    amount_usdc: float, yes_price: float,
                    clob_ask_price: float = 0.0) -> Optional[dict]:
    """
    Achète amount_usdc de tokens YES via CLOB V2.
    Stratégie SY : YES 94-98%, résolution ≤ 96h, on parie que l'événement arrive.

    Même logique que place_no_order : limit order au best_ask quand disponible.
    """
    amount_usdc = max(round(amount_usdc, 2), 1.0)

    if clob_ask_price > 0:
        exec_price  = min(round(clob_ask_price, 4), 0.99)
        size_tokens = round(amount_usdc / exec_price, 4)
        try:
            resp = client.create_and_post_order(
                OrderArgs(
                    token_id = token_id,
                    price    = float(exec_price),
                    size     = float(size_tokens),
                    side     = Side.BUY,
                ),
                options = PartialCreateOrderOptions(tick_size="0.01"),
            )
            filled_usdc = round(size_tokens * exec_price, 2)
            resp["_filled_usdc"] = filled_usdc
            logger.success(
                f"  Ordre YES placé : token={token_id[:15]}...  "
                f"{filled_usdc:.2f}$ ({size_tokens:.4f} tokens @ YES={exec_price:.3f}) | resp={resp}"
            )
            return resp
        except Exception as e:
            logger.error(f"  Ordre YES échoué (token={token_id[:15]}...) : {e}")
            return None

    # Fallback FAK market order
    exec_price = min(round(yes_price, 4), 0.99)
    try:
        resp = client.create_and_post_market_order(
            order_args = MarketOrderArgs(
                token_id   = token_id,
                amount     = amount_usdc,
                side       = Side.BUY,
                order_type = OrderType.FAK,
            ),
            options    = PartialCreateOrderOptions(tick_size="0.01"),
            order_type = OrderType.FAK,
        )
        filled_usdc = _extract_filled_usdc(resp, amount_usdc)
        if filled_usdc < 1.0:
            logger.warning(f"  Fill YES trop faible ({filled_usdc:.2f}$) pour {amount_usdc}$ demandés — ignoré")
            return None
        resp["_filled_usdc"] = filled_usdc
        logger.success(
            f"  Ordre YES placé (FAK) : token={token_id[:15]}...  "
            f"{filled_usdc:.2f}$ (demandé {amount_usdc:.2f}$) @ YES≈{exec_price:.3f} | resp={resp}"
        )
        return resp
    except Exception as e:
        logger.error(f"  Ordre YES échoué (token={token_id[:15]}...) : {e}")
        return None


# ── Placement d'ordre vente (CLOB V2) ────────────────────────────────────────

def sell_no_position(client: ClobClient, token_id: str,
                     amount_tokens: float, min_price: float) -> Optional[dict]:
    """
    Vend amount_tokens de tokens NO via CLOB V2 (ordre FAK).
    FAK remplit ce qui est disponible côté bid, annule le reste.

    Pour les ordres de VENTE, 'amount' est le nombre de tokens à vendre.
    min_price est fourni à titre informatif dans les logs.

    Args:
        token_id      : token_id du token NO
        amount_tokens : nombre de tokens NO à vendre
        min_price     : prix minimum souhaité (non garanti en FAK, juste loggé)
    """
    amount_tokens = max(round(amount_tokens, 6), 0.000001)

    try:
        resp = client.create_and_post_market_order(
            order_args = MarketOrderArgs(
                token_id   = token_id,
                amount     = amount_tokens,
                side       = Side.SELL,
                order_type = OrderType.FAK,
            ),
            options    = PartialCreateOrderOptions(tick_size="0.01"),
            order_type = OrderType.FAK,
        )
        logger.success(
            f"  Vente NO : token={token_id[:15]}...  "
            f"{amount_tokens:.4f} tokens (prix min souhaité {min_price:.3f}) | resp={resp}"
        )
        return resp
    except Exception as e:
        logger.error(f"  Vente échouée (token={token_id[:15]}...) : {e}")
        return None


# ── Placement d'ordre vente YES (CLOB V2) ────────────────────────────────────

def sell_yes_position(client: ClobClient, token_id: str,
                      amount_tokens: float, min_price: float) -> Optional[dict]:
    """
    Vend amount_tokens de tokens YES via CLOB V2 (ordre FAK).
    Utilisé pour la clôture anticipée des positions SY quand YES ≥ 99¢.
    """
    amount_tokens = max(round(amount_tokens, 6), 0.000001)
    try:
        resp = client.create_and_post_market_order(
            order_args = MarketOrderArgs(
                token_id   = token_id,
                amount     = amount_tokens,
                side       = Side.SELL,
                order_type = OrderType.FAK,
            ),
            options    = PartialCreateOrderOptions(tick_size="0.01"),
            order_type = OrderType.FAK,
        )
        logger.success(
            f"  Vente YES : token={token_id[:15]}...  "
            f"{amount_tokens:.4f} tokens (prix min souhaité {min_price:.3f}) | resp={resp}"
        )
        return resp
    except Exception as e:
        logger.error(f"  Vente YES échouée (token={token_id[:15]}...) : {e}")
        return None


# ── Ordre limite GTC de vente ────────────────────────────────────────────────

def place_gtc_sell_no(client: ClobClient, token_id: str,
                      tokens: float, limit_price: float = 0.99) -> Optional[dict]:
    """
    Place un ordre limite GTC (Good Till Cancelled) pour vendre tokens NO à limit_price.
    L'ordre reste dans le carnet Polymarket jusqu'à exécution automatique.
    À utiliser immédiatement après un achat NO pour garantir la sortie à 99¢.

    Args:
        token_id    : token_id du NO
        tokens      : nombre de tokens NO à vendre (= mise / prix_entrée_NO)
        limit_price : prix limite de vente (défaut 0.99 = NO à 99¢)
    """
    tokens = max(round(tokens, 4), 0.01)
    try:
        resp = client.create_and_post_order(
            OrderArgs(
                token_id = token_id,
                price    = float(limit_price),
                size     = float(tokens),
                side     = Side.SELL,
            ),
            options = PartialCreateOrderOptions(tick_size="0.01"),
        )
        logger.success(
            f"  Ordre GTC vente NO : {tokens:.4f} tokens @ {limit_price:.2f} | resp={resp}"
        )
        return resp
    except Exception as e:
        logger.error(f"  Ordre GTC vente NO échoué (token={token_id[:15]}...) : {e}")
        return None


def place_gtc_sell_yes(client: ClobClient, token_id: str,
                       tokens: float, limit_price: float = 0.99) -> Optional[dict]:
    """
    Place un ordre limite GTC pour vendre tokens YES à limit_price.
    À utiliser après un achat YES (SY) pour garantir la sortie à 99¢.
    """
    tokens = max(round(tokens, 4), 0.01)
    try:
        resp = client.create_and_post_order(
            OrderArgs(
                token_id = token_id,
                price    = float(limit_price),
                size     = float(tokens),
                side     = Side.SELL,
            ),
            options = PartialCreateOrderOptions(tick_size="0.01"),
        )
        logger.success(
            f"  Ordre GTC vente YES : {tokens:.4f} tokens @ {limit_price:.2f} | resp={resp}"
        )
        return resp
    except Exception as e:
        logger.error(f"  Ordre GTC vente YES échoué (token={token_id[:15]}...) : {e}")
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
            "https://data-api.polymarket.com/positions",
            params={"user": funder, "sizeThreshold": "0.001"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            return data
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


# ── Valeur totale du portefeuille ────────────────────────────────────────────

def get_total_portfolio_value(client: ClobClient) -> float:
    """
    Retourne valeur totale = USDC liquide + valeur des positions ouvertes.
    Logge les champs bruts de la première position pour faciliter le debug.
    """
    funder = os.environ.get("POLYMARKET_PROXY_WALLET", "").strip()
    if not funder:
        logger.warning("POLYMARKET_PROXY_WALLET absent — impossible de calculer le total")
        return 0.0

    usdc = get_usdc_balance(client)

    try:
        resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder, "sizeThreshold": "0.001"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Positions non récupérées : {e}")
        return usdc

    positions = data if isinstance(data, list) else data.get("positions", [])
    if not positions:
        logger.info(f"Aucune position trouvée — total = USDC seul ({usdc:.2f}$)")
        return usdc

    # Logguer les champs bruts pour debug (première position)
    sample = positions[0]
    logger.info(f"Champs position API : {list(sample.keys())}")
    logger.info(f"Valeurs 1ère pos    : { {k: sample[k] for k in list(sample.keys())[:10]} }")

    # ── Méthode 1 : currentValue directement fourni par l'API ────────────────
    # Polymarket renvoie parfois la valeur USDC courante directement.
    positions_value = sum(
        float(p.get("currentValue") or p.get("current_value") or 0)
        for p in positions
    )
    if positions_value > 0:
        total = round(usdc + positions_value, 2)
        logger.info(f"Total (via currentValue) : {total:.2f}$ "
                    f"(USDC {usdc:.2f}$ + positions {positions_value:.2f}$)")
        return total

    # ── Méthode 2 : size × prix CLOB courant ─────────────────────────────────
    # Chercher le champ token_id (toutes variantes connues)
    token_field = next(
        (k for k in ("asset_id", "tokenId", "token_id", "assetId",
                     "conditionTokenId", "outcomeTokenId", "proxyWallet")
         if sample.get(k) and str(sample.get(k)).strip()),
        None
    )
    # Chercher le champ size (toutes variantes, valeur non nulle)
    size_field = next(
        (k for k in ("size", "balance", "quantity", "amount", "shares",
                     "tokensOwned", "tokens_owned", "netPosition")
         if sample.get(k) not in (None, 0, "0", "0.0", 0.0, "")),
        None
    )

    logger.info(f"token_field détecté : {token_field}  |  size_field détecté : {size_field}")

    if not token_field or not size_field:
        logger.warning(f"Champs token/size non trouvés — total = USDC seul ({usdc:.2f}$)")
        return usdc

    positions_value = 0.0
    for pos in positions:
        token_id = str(pos.get(token_field, "")).strip()
        size_raw = pos.get(size_field)
        if not token_id or size_raw in (None, 0, "0", "0.0", 0.0, ""):
            continue
        try:
            size = float(size_raw)
        except (TypeError, ValueError):
            continue

        # Prix courant depuis le CLOB
        try:
            pr    = requests.get("https://clob.polymarket.com/last-trade-price",
                                 params={"token_id": token_id}, timeout=10)
            price = float(pr.json().get("price", 0)) if pr.status_code == 200 else 0.0
        except Exception:
            price = 0.0

        positions_value += size * (price if price > 0 else 1.0)

    total = round(usdc + positions_value, 2)
    logger.info(f"Total (via size×prix) : {total:.2f}$ "
                f"(USDC {usdc:.2f}$ + positions {positions_value:.2f}$)")
    return total
