"""
polymarket_common/client.py
============================
Instanciation du ClobClient. Importé par bot_directional et bot_lp.
Chaque bot passe ses propres credentials — un client par wallet.

Adapté à l'audit du bot existant (live_bot.py / order_executor.py) :
  - SDK déjà utilisé en prod : py-clob-client-v2 (CLOB v2, post-migration
    avril 2026). On le conserve ici pour ne rien casser côté bot_directional.
  - Wallet directionnel = proxy Gnosis Safe Polymarket :
        signature_type=2 (GNOSIS_SAFE) + funder=POLYMARKET_PROXY_WALLET
    C'est différent du wallet EOA MetaMask "neuf" (signature_type=0) prévu
    pour le bot LP dans polymarket_common_setup.md. Les deux sont supportés
    par py-clob-client-v2 via le paramètre signature_type.

Variables d'environnement attendues :
  Bot directionnel (existant) :
    POLYMARKET_PRIVATE_KEY
    POLYMARKET_API_KEY / POLYMARKET_API_SECRET / POLYMARKET_API_PASSPHRASE
    POLYMARKET_PROXY_WALLET   (adresse du proxy Gnosis Safe)

  Bot LP (nouveau wallet MetaMask EOA) :
    LP_PRIVATE_KEY
    LP_POLY_API_KEY / LP_POLY_API_SECRET / LP_POLY_API_PASSPHRASE
    (pas de funder : signature_type=0, EOA direct)
"""

import os
from typing import Optional

from py_clob_client_v2 import ClobClient, ApiCreds

HOST     = "https://clob.polymarket.com"
CHAIN_ID = 137


def build_client(
    private_key: str,
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    signature_type: int = 2,
    funder: Optional[str] = None,
) -> ClobClient:
    """
    Construit un ClobClient authentifié (py-clob-client-v2).

    signature_type=2 (GNOSIS_SAFE) : wallet proxy Polymarket — fournir `funder`.
    signature_type=0 (EOA)         : wallet MetaMask direct — pas de `funder`.
    """
    creds = ApiCreds(
        api_key        = api_key,
        api_secret     = api_secret,
        api_passphrase = api_passphrase,
    )
    return ClobClient(
        host           = HOST,
        chain_id       = CHAIN_ID,
        key            = private_key,
        creds          = creds,
        signature_type = signature_type,
        funder         = funder,
    )


def build_directional_client() -> ClobClient:
    """
    Client pour le bot directionnel existant — wallet proxy Gnosis Safe.
    Reproduit exactement build_client() de order_executor.py (signature_type=2
    + funder=POLYMARKET_PROXY_WALLET) pour préserver le comportement actuel.
    """
    return build_client(
        private_key    = os.environ["POLYMARKET_PRIVATE_KEY"],
        api_key        = os.environ["POLYMARKET_API_KEY"],
        api_secret     = os.environ["POLYMARKET_API_SECRET"],
        api_passphrase = os.environ["POLYMARKET_API_PASSPHRASE"],
        signature_type = 2,
        funder         = os.environ.get("POLYMARKET_PROXY_WALLET", "").strip() or None,
    )


def build_lp_client() -> ClobClient:
    """
    Client pour le bot LP — nouveau wallet MetaMask EOA dédié.
    signature_type=0, pas de funder (wallet direct, pas de proxy).
    """
    return build_client(
        private_key    = os.environ["LP_PRIVATE_KEY"],
        api_key        = os.environ["LP_POLY_API_KEY"],
        api_secret     = os.environ["LP_POLY_API_SECRET"],
        api_passphrase = os.environ["LP_POLY_API_PASSPHRASE"],
        signature_type = 0,
        funder         = None,
    )


def create_api_keys(private_key: str, signature_type: int = 0,
                    funder: Optional[str] = None) -> dict:
    """
    Génère ou retrouve les credentials API L2 depuis une clé privée.
    Script one-time, à lancer une seule fois par wallet.

    Pour le wallet directionnel existant (proxy Gnosis Safe), passer
    signature_type=2 et funder=<adresse proxy>. Pour un nouveau wallet
    MetaMask EOA (bot LP), les valeurs par défaut conviennent.
    """
    client = ClobClient(
        host           = HOST,
        chain_id       = CHAIN_ID,
        key            = private_key,
        signature_type = signature_type,
        funder         = funder,
    )
    creds = client.create_or_derive_api_creds()
    return {
        "api_key":        creds.api_key,
        "api_secret":     creds.api_secret,
        "api_passphrase": creds.passphrase,
    }
