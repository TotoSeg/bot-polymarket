"""
Phase 5 — Paper Trading Polymarket
====================================
Simule le bot en temps réel SANS argent réel :
  1. Récupère les marchés actifs via l'API Gamma
  2. Détecte les signaux (stratégies S1-S6)
  3. Enregistre les positions virtuelles (avec Kelly sizing)
  4. Vérifie la résolution des marchés ouverts
  5. Calcule le P&L et sauvegarde l'état

Le fichier outputs/phase5/portfolio.json persiste entre les runs :
  → Relancer le script = update les positions existantes + chercher de nouvelles

Usage :
    # Un seul scan (cron ou test)
    python src/phase5_paper/paper_trading.py

    # Afficher uniquement le résumé du portefeuille
    python src/phase5_paper/paper_trading.py --report

    # Scanner en boucle toutes les N heures
    python src/phase5_paper/paper_trading.py --loop --interval 6
"""

import sys
import time
import argparse
from pathlib import Path
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from loguru import logger

# Import des modules phase 5
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.phase5_paper.polymarket_client import (
    get_active_markets, get_market, parse_yes_price, parse_resolution
)
from src.phase5_paper.strategy_signals import check_signals
from src.phase5_paper.paper_portfolio import (
    load_portfolio, save_portfolio,
    add_position, close_position, print_summary,
)

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOGS_DIR     = PROJECT_ROOT / "logs"
OUT_DIR      = PROJECT_ROOT / "outputs" / "phase5"
PORTFOLIO_FILE = OUT_DIR / "portfolio.json"
TRADES_CSV     = OUT_DIR / "paper_trades.csv"


def setup_logger():
    LOGS_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_paper_trading.log"
    logger.remove()
    logger.add(sys.stdout, colorize=True, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add(log_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
    return log_file


# ── 1. Résoudre les positions ouvertes ────────────────────────────────────────

def resolve_open_positions(portfolio: dict):
    """
    Pour chaque position ouverte, vérifie si le marché est résolu.
    Si oui, ferme la position et calcule le P&L.
    """
    open_ids = list(portfolio["positions_ouvertes"].keys())
    if not open_ids:
        logger.info("Aucune position ouverte a verifier.")
        return 0

    logger.info(f"Verification de {len(open_ids)} positions ouvertes...")
    nb_closed = 0

    for mid in open_ids:
        market = get_market(mid)
        if market is None:
            continue

        outcome = parse_resolution(market)
        if outcome is not None:
            close_position(portfolio, mid, outcome)
            nb_closed += 1

        time.sleep(0.1)  # Rate limiting

    logger.info(f"  {nb_closed} position(s) fermee(s) ce run.")
    return nb_closed


# ── 2. Scanner les marchés actifs pour de nouveaux signaux ───────────────────

def scan_new_signals(portfolio: dict):
    """
    Récupère les marchés actifs et cherche de nouveaux signaux.
    N'ajoute pas de positions pour les marchés déjà en portefeuille.
    """
    logger.info("Scan des marches actifs...")
    markets = get_active_markets(min_volume=500.0, max_pages=30)

    already_open = set(portfolio["positions_ouvertes"].keys())
    already_closed = {t["market_id"] for t in portfolio["trades_clos"]}
    already_seen   = already_open | already_closed

    nb_new     = 0
    nb_scanned = 0
    strategy_counts = {}

    for market in markets:
        mid = str(market.get("id", ""))
        if mid in already_seen:
            continue  # Déjà traité

        yes_price = parse_yes_price(market)
        if yes_price is None or yes_price <= 0 or yes_price >= 1:
            continue

        nb_scanned += 1
        signals = check_signals(market, yes_price)

        if not signals:
            continue

        # Prendre le signal le plus fort (stratégie la plus précise)
        # Priorité : S5 > S2 > S3 > S4 > S6 > S1
        PRIORITY = {"S5": 6, "S2": 5, "S3": 4, "S4": 3, "S6": 2, "S1": 1}
        best = max(signals, key=lambda s: PRIORITY.get(s["strategy"], 0))

        added = add_position(
            portfolio=portfolio,
            market=market,
            strategy=best["strategy"],
            win_rate_prior=best["win_rate_prior"],
            entry_price_yes=yes_price,
            reason=best["reason"],
        )

        if added:
            nb_new += 1
            strategy_counts[best["strategy"]] = strategy_counts.get(best["strategy"], 0) + 1

    logger.info(f"  Marchés scannés : {nb_scanned:,} | Nouvelles positions : {nb_new}")
    if strategy_counts:
        for strat, cnt in sorted(strategy_counts.items()):
            logger.info(f"    {strat} : +{cnt} position(s)")


# ── 3. Export CSV ─────────────────────────────────────────────────────────────

def export_trades_csv(portfolio: dict):
    """Exporte tous les trades clos en CSV pour analyse."""
    clos = portfolio["trades_clos"]
    if not clos:
        return
    df = pd.DataFrame(clos)
    df.to_csv(TRADES_CSV, index=False, encoding="utf-8")
    logger.info(f"Trades exportes : {TRADES_CSV.relative_to(PROJECT_ROOT)}")


# ── 4. Graphique évolution P&L ────────────────────────────────────────────────

def plot_pnl(portfolio: dict):
    """Génère un graphique de l'évolution du capital (P&L cumulé)."""
    clos = portfolio["trades_clos"]
    if len(clos) < 2:
        return

    df = pd.DataFrame(clos)
    df["exit_date"] = pd.to_datetime(df["exit_date"])
    df = df.sort_values("exit_date")
    df["pnl_cumul"] = df["profit_net"].cumsum()
    df["capital"] = portfolio["capital_initial"] + df["pnl_cumul"]

    fig, axes = plt.subplots(2, 1, figsize=(13, 8))
    fig.suptitle("Paper Trading — Évolution du capital", fontsize=13)

    axes[0].plot(df["exit_date"], df["capital"], color="steelblue", linewidth=2)
    axes[0].axhline(portfolio["capital_initial"], color="gray", linestyle="--", label="Capital initial")
    axes[0].set_ylabel("Capital ($)")
    axes[0].set_title("Capital au fil du temps")
    axes[0].legend()

    # Barres par stratégie
    from collections import defaultdict
    strat_pnl = defaultdict(float)
    for t in clos:
        strat_pnl[t["strategy"]] += t["profit_net"]

    strategies = sorted(strat_pnl.keys())
    pnls       = [strat_pnl[s] for s in strategies]
    colors     = ["green" if p >= 0 else "red" for p in pnls]
    axes[1].bar(strategies, pnls, color=colors)
    axes[1].axhline(0, color="black", linewidth=1)
    axes[1].set_ylabel("P&L total ($)")
    axes[1].set_title("P&L cumulé par stratégie")
    for i, (s, p) in enumerate(zip(strategies, pnls)):
        axes[1].text(i, p + (0.5 if p >= 0 else -0.5), f"{p:+.1f}$",
                     ha="center", fontsize=9)

    plt.tight_layout()
    out_path = OUT_DIR / "paper_pnl.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.success(f"Graphique : {out_path.relative_to(PROJECT_ROOT)}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def run_once():
    """Exécute un cycle complet : résolution + scan + export."""
    logger.info("=" * 60)
    logger.info(f"PAPER TRADING — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    portfolio = load_portfolio(PORTFOLIO_FILE)

    # 1. Résoudre les positions déjà ouvertes
    resolve_open_positions(portfolio)

    # 2. Scanner les nouveaux signaux
    scan_new_signals(portfolio)

    # 3. Sauvegarder et exporter
    save_portfolio(portfolio, PORTFOLIO_FILE)
    export_trades_csv(portfolio)

    # 4. Rapport + graphique
    print_summary(portfolio)
    plot_pnl(portfolio)

    logger.success("Cycle termine.")


def main():
    log_file = setup_logger()

    parser = argparse.ArgumentParser(description="Paper trading Polymarket")
    parser.add_argument("--report",   action="store_true",
                        help="Afficher uniquement le résumé du portefeuille")
    parser.add_argument("--loop",     action="store_true",
                        help="Tourner en boucle continue")
    parser.add_argument("--interval", type=float, default=6.0,
                        help="Intervalle entre les scans en heures (défaut : 6)")
    args = parser.parse_args()

    if args.report:
        # Afficher seulement le résumé sans scanner
        portfolio = load_portfolio(PORTFOLIO_FILE)
        print_summary(portfolio)
        return

    if args.loop:
        logger.info(f"Mode boucle : scan toutes les {args.interval}h")
        while True:
            run_once()
            wait_s = int(args.interval * 3600)
            logger.info(f"Prochain scan dans {args.interval}h (Ctrl+C pour arreter)")
            time.sleep(wait_s)
    else:
        run_once()


if __name__ == "__main__":
    main()
