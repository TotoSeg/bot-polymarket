"""
polymarket_common/allowances.py
============================
Approbations one-time des contrats Polymarket pour un wallet EOA MetaMask.

Le bot directionnel existant utilise un wallet PROXY Gnosis Safe
(signature_type=2) — les wallets proxy n'ont pas besoin de cette étape,
les allowances sont gérées par l'interface Polymarket à la création du proxy.

Ce module concerne uniquement le NOUVEAU wallet LP (signature_type=0, EOA).
À appeler une seule fois après create_api_keys(), avant le premier ordre.
"""

from py_clob_client_v2 import ClobClient, AssetType


def set_allowances(client: ClobClient) -> None:
    """
    Approuve USDC/pUSD + tokens conditionnels (YES/NO) pour les contrats
    CTF Exchange. Coût : ~0.05 $ de gas Polygon. Une seule fois par wallet EOA.
    """
    resp = client.set_allowance(asset_type=AssetType.COLLATERAL)
    print(f"Allowance COLLATERAL : {resp}")

    resp = client.set_allowance(asset_type=AssetType.CONDITIONAL)
    print(f"Allowance CONDITIONAL : {resp}")

    print("Allowances configurées. Ne relancer cette fonction qu'en cas de reset.")


if __name__ == "__main__":
    # Usage : python -m polymarket_common.allowances
    from polymarket_common.client import build_lp_client

    set_allowances(build_lp_client())
