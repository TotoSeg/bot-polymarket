"""
Phase 5 — Paper Trading S3 uniquement
======================================
Wallet fictif 1 000 $ — stratégie S3 seule (YES entre 5 % et 10 %).

Rappel backtest S3 :
  - WR historique : 98.1 % (2111 marchés, 2021-2026)
  - IC95 bootstrap : [98.3 %, 99.2 %]
  - Walk-forward out-of-sample : 99.2 %
  - Sharpe : 5.68 | ROI backtest : +441 %

Ce script tourne indépendamment du portfolio principal (portfolio_s3.json).
Usage :
    python src/phase5_paper/paper_trading_s3.py            # un cycle
    python src/phase5_paper/paper_trading_s3.py --report   # résumé sans scan
    python src/phase5_paper/paper_trading_s3.py --loop     # toutes les heures
"""

import sys, time, argparse
from pathlib import Path
from datetime import datetime, timezone
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.phase5_paper.polymarket_client import get_active_markets, parse_yes_price, parse_resolution, get_market
from src.phase5_paper.strategy_signals   import check_signals
from src.phase5_paper.paper_portfolio    import (
    load_portfolio, save_portfolio, add_position, close_position, print_summary
)

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT   = Path(__file__).resolve().parents[2]
OUT_DIR        = PROJECT_ROOT / "outputs" / "phase5"
PORTFOLIO_FILE = OUT_DIR / "portfolio_s3.json"
LOGS_DIR       = PROJECT_ROOT / "logs"


def setup_logger():
    LOGS_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    logger.remove()
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_paper_s3.log"
    logger.add(sys.stdout, colorize=True, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add(log_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")


def resolve_open_positions(portfolio: dict) -> int:
    """Ferme les positions S3 dont le marché est résolu."""
    positions = dict(portfolio["positions_ouvertes"])
    nb_closed = 0
    for mid, pos in positions.items():
        market = get_market(mid)
        if market is None:
            continue
        outcome = parse_resolution(market)
        if outcome is None:
            continue
        close_position(portfolio, mid, outcome)
        nb_closed += 1
        time.sleep(0.1)
    return nb_closed


def run_once():
    portfolio = load_portfolio(PORTFOLIO_FILE)

    logger.info("=" * 60)
    logger.info(f"PAPER TRADING S3 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 60)

    # 1. Résoudre les positions fermées
    nb_closed = resolve_open_positions(portfolio)
    logger.info(f"  {nb_closed} position(s) fermée(s) ce run.")

    # 2. Scanner les marchés actifs
    logger.info("Scan des marchés actifs...")
    markets = get_active_markets(min_volume=500, max_pages=30)
    logger.info(f"  {len(markets):,} marchés récupérés")

    # 3. Filtrer et entrer uniquement sur S3
    nb_new = 0
    candidates = []
    for m in markets:
        yp = parse_yes_price(m)
        if yp is None or not (0.05 <= yp <= 0.10):
            continue
        sigs = check_signals(m, yp)
        for s in sigs:
            if s["strategy"] == "S3":
                candidates.append((s["score"], m, s))

    # Trier par score décroissant pour prioriser les meilleurs signaux
    candidates.sort(key=lambda x: -x[0])

    for score, m, sig in candidates:
        added = add_position(
            portfolio      = portfolio,
            market         = m,
            strategy       = "S3",
            win_rate_prior = sig["win_rate_prior"],
            entry_price_yes= parse_yes_price(m),
            reason         = sig["reason"],
        )
        if added:
            nb_new += 1

    logger.info(f"  Nouvelles positions S3 : {nb_new} | Total candidates : {len(candidates)}")

    # 4. Sauvegarder et afficher le résumé
    save_portfolio(portfolio, PORTFOLIO_FILE)
    print_summary(portfolio)
    logger.success("Cycle S3 terminé.")


def main():
    setup_logger()
    parser = argparse.ArgumentParser(description="Paper trading S3 only")
    parser.add_argument("--report", action="store_true", help="Afficher le rapport sans scanner")
    parser.add_argument("--loop",   action="store_true", help="Tourner toutes les heures")
    args = parser.parse_args()

    if args.report:
        portfolio = load_portfolio(PORTFOLIO_FILE)
        print_summary(portfolio)
        return

    if args.loop:
        logger.info("Mode boucle : scan toutes les heures")
        while True:
            run_once()
            logger.info("Prochain scan dans 60 minutes")
            time.sleep(3600)
    else:
        run_once()


if __name__ == "__main__":
    main()
