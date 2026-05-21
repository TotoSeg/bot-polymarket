"""
Analyse des marchés disponibles par fenêtre de résolution.
Répond à la question : combien de marchés S3+SP (hors sport+crypto)
se résolvent dans les 48h, 72h, 96h, 7j, 14j ?

Usage :
    python src/phase6_bot/analyze_windows.py
"""

import sys
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.phase5_paper.polymarket_client import get_active_markets, parse_yes_price
from src.phase5_paper.strategy_signals import check_signals, _is_crypto
from src.phase5_paper.paper_portfolio import _kelly_size, load_portfolio

# Charger .env
_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

PORTFOLIO_FILE = Path(__file__).resolve().parents[2] / "outputs" / "phase6" / "live_portfolio.json"

# Mots-clés sport à exclure
_KW_SPORT = [
    "football", "soccer", "basketball", "tennis", "nba", "nfl", "nhl", "mlb",
    "premier league", "ligue 1", "serie a", "bundesliga", "la liga",
    "champions league", "world cup", "copa ", "super bowl",
    "goal scorer", "top scorer", "top goal", "golden boot",
    "tournament", "grand slam", "wimbledon", "formula 1", " f1 ",
    "ufc ", "boxing", "olympics", "rugby", "cricket", "golf",
    " fc ", "batting", "pitcher", "quarterback",
]

WINDOWS = [
    ("48h",  48),
    ("72h",  72),
    ("96h",  96),
    ("5j",   120),
    ("7j",   168),
    ("14j",  336),
]


def _is_sport(q: str) -> bool:
    return any(k in q for k in _KW_SPORT)


def main():
    now = datetime.now(tz=timezone.utc)

    # Capital Kelly actuel
    try:
        pf = load_portfolio(PORTFOLIO_FILE)
        capital_commit = sum(p["bet_amount"] for p in pf["positions_ouvertes"].values())
        capital_kelly  = pf["capital_disponible"] + capital_commit
    except Exception:
        capital_kelly = 285.0
        print("[INFO] Portfolio non trouvé, capital hypothétique = 285$")

    max_bet = float(os.getenv("MAX_BET_USDC", "200"))

    print(f"\nCapital Kelly total : {capital_kelly:.2f}$")
    print(f"MAX_BET_USDC        : {max_bet:.0f}$")
    print(f"Date actuelle       : {now.strftime('%Y-%m-%d %H:%M')} UTC\n")

    print("Récupération des marchés actifs (peut prendre 30 sec)...")
    markets = get_active_markets(min_volume=500, max_pages=30)

    # Filtrer et évaluer chaque marché
    eligible = []
    for m in markets:
        q   = str(m.get("question", "")).lower()
        yp  = parse_yes_price(m)
        if not yp or not (0.05 <= yp <= 0.35):
            continue
        if _is_crypto(q) or _is_sport(q):
            continue

        # Date de résolution
        raw = m.get("endDate") or ""
        if not raw:
            continue
        try:
            raw = raw.rstrip("Z").replace("Z", "+00:00")
            end_dt = datetime.fromisoformat(raw + ("T00:00:00" if "T" not in raw else ""))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        hours_to_end = (end_dt - now).total_seconds() / 3600
        if hours_to_end < 0:
            continue   # déjà expiré

        sigs = check_signals(m, yp)
        if not sigs:
            continue

        best_sig = max(sigs, key=lambda s: s["score"])
        bet = min(_kelly_size(best_sig["win_rate_prior"], yp, capital_kelly), max_bet)

        eligible.append({
            "question":   str(m.get("question", ""))[:70],
            "yp":         yp,
            "strategy":   best_sig["strategy"],
            "win_rate":   best_sig["win_rate_prior"],
            "bet":        bet,
            "hours":      hours_to_end,
            "end_dt":     end_dt,
        })

    eligible.sort(key=lambda x: x["hours"])

    # Rapport par fenêtre
    print("=" * 65)
    print(f"{'Fenêtre':<8} {'Marchés':>8} {'Bets>1$':>8} {'Capital déployé':>16} {'% capital':>10}")
    print("=" * 65)

    prev_h = 0
    cumul_deployed = 0.0
    running_capital = capital_kelly

    for label, h in WINDOWS:
        subset   = [e for e in eligible if prev_h < e["hours"] <= h]
        bets_ok  = [e for e in subset if e["bet"] >= 1.0]
        deployed = sum(min(e["bet"], running_capital) for e in bets_ok)
        deployed = min(deployed, running_capital)
        cumul_deployed += deployed
        running_capital = max(0, capital_kelly - cumul_deployed)
        pct = cumul_deployed / capital_kelly * 100 if capital_kelly > 0 else 0
        print(f"{label:<8} {len(subset):>8} {len(bets_ok):>8} {cumul_deployed:>14.2f}$ {pct:>9.1f}%")
        prev_h = h

    print("=" * 65)

    # Détail 48h
    subset_48 = [e for e in eligible if e["hours"] <= 48]
    if subset_48:
        print(f"\nDétail des {len(subset_48)} marchés résolvant dans les 48h :")
        print(f"{'Stratégie':<5} {'YES':>5} {'Bet':>6}  {'Résolution':<12}  Question")
        print("-" * 80)
        for e in subset_48[:30]:
            end_str = e["end_dt"].strftime("%m-%d %H:%M")
            print(f"{e['strategy']:<5} {e['yp']:>5.3f} {e['bet']:>5.2f}$  {end_str:<12}  {e['question'][:45]}")
        if len(subset_48) > 30:
            print(f"  ... et {len(subset_48)-30} autres")
    else:
        print("\nAucun marché éligible dans les 48h.")

    print()


if __name__ == "__main__":
    main()
