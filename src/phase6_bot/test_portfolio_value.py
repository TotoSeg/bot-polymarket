"""
Test de get_total_portfolio_value()
Comparer la valeur retournée avec ce qu'affiche Polymarket.

Usage : python3 src/phase6_bot/test_portfolio_value.py
"""
import os, json, requests
from pathlib import Path

# Charger .env
_env = Path(__file__).parent / ".env"
for line in _env.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from src.phase6_bot.order_executor import build_client, get_usdc_balance, get_total_portfolio_value

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

client = build_client()

# ── 1. USDC liquide ──────────────────────────────────────────────────────────
usdc = get_usdc_balance(client)
print(f"USDC disponible        : {usdc:.2f}$")

# ── 2. Positions brutes (pour debug des champs) ──────────────────────────────
funder = os.environ.get("POLYMARKET_PROXY_WALLET", "").strip()
resp = requests.get("https://data-api.polymarket.com/positions",
                    params={"user": funder, "sizeThreshold": "0.001"}, timeout=15)
positions = resp.json()
if isinstance(positions, dict):
    positions = positions.get("positions", [])

print(f"Positions trouvées     : {len(positions)}")
if positions:
    print(f"Champs disponibles     : {list(positions[0].keys())}")
    print()
    for p in positions:
        print(f"  {json.dumps(p)}")

# ── 3. Valeur totale calculée ────────────────────────────────────────────────
print()
total = get_total_portfolio_value(client)
print(f"\nTotal calculé par bot  : {total:.2f}$")
print(f"→ Comparer avec la valeur affichée sur polymarket.com")
