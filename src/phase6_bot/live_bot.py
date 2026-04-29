"""
Phase 6 — Bot de trading réel Polymarket (stratégie S3)
========================================================
Tourne en boucle toutes les heures, scanne les marchés S3 (YES 5-10%)
et place des ordres réels via le CLOB Polymarket.

PRÉREQUIS avant de lancer :
  1. Copier src/phase6_bot/.env.example → src/phase6_bot/.env et remplir
  2. Déposer des USDC sur le wallet Polygon (adresse dans .env)
  3. Générer les clés API : python src/phase6_bot/live_bot.py --create-keys
  4. Tester en dry-run d'abord : python src/phase6_bot/live_bot.py --dry-run

Usage :
    python src/phase6_bot/live_bot.py --create-keys  # étape 1 config
    python src/phase6_bot/live_bot.py --status        # solde + positions
    python src/phase6_bot/live_bot.py --dry-run       # scan sans ordres réels
    python src/phase6_bot/live_bot.py                 # un cycle réel
    python src/phase6_bot/live_bot.py --loop          # boucle toutes les heures
"""

import sys, os, time, json, argparse
from pathlib import Path
from datetime import datetime, timezone

# Charger .env automatiquement si présent
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from loguru import logger
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.phase5_paper.polymarket_client import get_active_markets, parse_yes_price, parse_resolution, get_market
from src.phase5_paper.strategy_signals   import check_signals
from src.phase5_paper.paper_portfolio    import (
    load_portfolio, save_portfolio, add_position, close_position, print_summary,
    _kelly_size,
)
from src.phase6_bot.order_executor import (
    build_client, create_api_keys, get_no_token_id, check_liquidity,
    place_no_order, get_usdc_balance,
)

PROJECT_ROOT   = Path(__file__).resolve().parents[2]
OUT_DIR        = PROJECT_ROOT / "outputs" / "phase6"
PORTFOLIO_FILE = OUT_DIR / "live_portfolio.json"
LOGS_DIR       = PROJECT_ROOT / "logs"


def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    logger.remove()
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_live_bot.log"
    logger.add(sys.stdout, colorize=True, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add(log_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")


def run_once(dry_run: bool = False):
    """
    Un cycle complet :
      1. Résoudre les positions dont le marché est clos
      2. Scanner les marchés S3
      3. Placer les ordres (ou simuler si dry_run=True)
    """
    portfolio = load_portfolio(PORTFOLIO_FILE)
    client    = None if dry_run else build_client()

    logger.info("=" * 60)
    logger.info(f"LIVE BOT S3 — {datetime.now().strftime('%Y-%m-%d %H:%M')} "
                f"{'[DRY-RUN]' if dry_run else '[RÉEL]'}")
    logger.info("=" * 60)

    # ── Afficher le solde réel ────────────────────────────────────────────────
    if not dry_run and client:
        balance = get_usdc_balance(client)
        logger.info(f"Solde USDC wallet : {balance:.2f} USDC")

    # ── 1. Résoudre les positions ouvertes ────────────────────────────────────
    nb_closed = 0
    for mid in list(portfolio["positions_ouvertes"].keys()):
        market = get_market(mid)
        if market is None:
            continue
        outcome = parse_resolution(market)
        if outcome is None:
            continue
        close_position(portfolio, mid, outcome)
        nb_closed += 1
        time.sleep(0.1)
    logger.info(f"Positions fermées ce cycle : {nb_closed}")

    # ── 2. Scanner les marchés S3 ─────────────────────────────────────────────
    logger.info("Scan des marchés actifs...")
    markets = get_active_markets(min_volume=int(os.getenv("MIN_VOLUME_USD", "500")),
                                 max_pages=30)

    max_bet  = float(os.getenv("MAX_BET_USDC", "25"))
    nb_new   = 0
    skipped  = 0

    # Collecter et trier par score décroissant
    candidates = []
    for m in markets:
        yp = parse_yes_price(m)
        if not yp or not (0.05 <= yp <= 0.10):
            continue
        sigs = check_signals(m, yp)
        for s in sigs:
            if s["strategy"] == "S3":
                candidates.append((s["score"], m, s, yp))
    candidates.sort(key=lambda x: -x[0])

    for score, m, sig, yp in candidates:
        mid = str(m.get("id", ""))

        # Déjà en portefeuille
        if mid in portfolio["positions_ouvertes"]:
            continue

        # Calculer la mise Kelly (plafonnée à MAX_BET_USDC)
        bet = min(
            _kelly_size(sig["win_rate_prior"], yp, portfolio["capital_initial"]),
            max_bet,
        )
        if bet <= 0 or bet > portfolio["capital_disponible"]:
            skipped += 1
            continue

        # Récupérer le token NO
        no_token = get_no_token_id(m)
        if no_token is None:
            logger.debug(f"Pas de token_id NO pour {mid[:10]}... — skipped")
            skipped += 1
            continue

        if dry_run:
            # Simuler sans placer d'ordre réel
            logger.info(f"  [DRY-RUN] S3 | {m.get('question','')[:50]:50s} | "
                        f"YES={yp:.3f} | NO_token={no_token[:12]}... | Mise={bet:.2f}$")
            add_position(portfolio, m, "S3", sig["win_rate_prior"], yp, sig["reason"])
            nb_new += 1
        else:
            # Vérifier liquidité puis placer l'ordre
            if not check_liquidity(client, no_token, bet):
                skipped += 1
                continue
            resp = place_no_order(client, no_token, bet, yp)
            if resp:
                # Enregistrer la position (avec order_id pour suivi)
                m["_order_id"] = resp.get("orderID", "")
                add_position(portfolio, m, "S3", sig["win_rate_prior"], yp, sig["reason"])
                nb_new += 1
            else:
                skipped += 1
            time.sleep(0.3)  # respecter les rate limits CLOB

    logger.info(f"Nouvelles positions : {nb_new} | Skipped : {skipped}")

    # ── 3. Sauvegarder et afficher ────────────────────────────────────────────
    save_portfolio(portfolio, PORTFOLIO_FILE)
    print_summary(portfolio)
    logger.success("Cycle terminé.")


# ── Commandes utilitaires ──────────────────────────────────────────────────────

def cmd_create_keys():
    """Génère et affiche les clés API à partir de la clé privée."""
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk or pk == "0x_VOTRE_CLE_PRIVEE_ICI":
        logger.error("Remplir POLYMARKET_PRIVATE_KEY dans .env avant de générer les clés")
        return
    logger.info("Génération des clés API Polymarket...")
    creds = create_api_keys(pk)
    logger.success("Clés générées — copier dans .env :")
    for k, v in creds.items():
        print(f"  POLYMARKET_{k.upper()}={v}")


def cmd_status():
    """Affiche le solde et les positions ouvertes."""
    try:
        client  = build_client()
        balance = get_usdc_balance(client)
        logger.info(f"Solde USDC wallet : {balance:.2f} USDC")
    except Exception as e:
        logger.warning(f"Solde non disponible (clés API requises) : {e}")
    portfolio = load_portfolio(PORTFOLIO_FILE)
    print_summary(portfolio)


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    setup()
    parser = argparse.ArgumentParser(description="Live bot Polymarket S3")
    parser.add_argument("--create-keys", action="store_true", help="Générer les clés API")
    parser.add_argument("--status",      action="store_true", help="Solde + positions")
    parser.add_argument("--dry-run",     action="store_true", help="Simuler sans ordres réels")
    parser.add_argument("--loop",        action="store_true", help="Boucle toutes les heures")
    args = parser.parse_args()

    if args.create_keys:
        cmd_create_keys()
        return

    if args.status:
        cmd_status()
        return

    if args.loop:
        logger.info("Mode boucle : scan toutes les heures")
        while True:
            try:
                run_once(dry_run=args.dry_run)
            except Exception as e:
                logger.error(f"Erreur cycle : {e}")
            logger.info("Prochain cycle dans 60 minutes")
            time.sleep(3600)
    else:
        run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
