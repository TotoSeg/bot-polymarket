"""
Phase 6 – Bot de trading réel Polymarket (stratégies S3 + SP)
==============================================================
Tourne en boucle toutes les heures, scanne les marchés :
  S3 : YES 5-10%, tous marchés hors crypto (WR 99.7%)
  SP : YES 5-35%, marchés politiques/géopolitiques (WR 94.9%)
et place des ordres réels de type NO via le CLOB Polymarket.

Règles actives :
  1. Gain attendu minimum 5% (EV = win_rate × gain_si_NO_gagne − perte_si_NO_perd)
  2. Clôture anticipée si la certitude de gain atteint 99.9% (YES ≤ 0.1%)
     uniquement si la liquidité permet de sortir sans P&L négatif
  3. Positions uniquement sur les marchés résolvant dans les 14 jours
  4. Reporting Notion automatique toutes les 30 minutes (entre les cycles)

Usage :
    python src/phase6_bot/live_bot.py --create-keys   # étape 1 config
    python src/phase6_bot/live_bot.py --status        # solde + positions
    python src/phase6_bot/live_bot.py --dry-run       # scan sans ordres réels
    python src/phase6_bot/live_bot.py                 # un cycle réel
    python src/phase6_bot/live_bot.py --loop          # boucle toutes les heures + Notion 30min
    python src/phase6_bot/live_bot.py --cleanup       # ferme positions hors règles + résolution > 31/05
"""

import sys, os, time, json, argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone

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
    check_sell_liquidity, sell_no_position,
)
from src.phase6_bot.notion_reporter import update_notion_report

PROJECT_ROOT   = Path(__file__).resolve().parents[2]
OUT_DIR        = PROJECT_ROOT / "outputs" / "phase6"
PORTFOLIO_FILE = OUT_DIR / "live_portfolio.json"
LOGS_DIR       = PROJECT_ROOT / "logs"

# Frais Polymarket : 2% du profit
POLYMARKET_FEE = 0.02

# Seuil de gain attendu minimum pour entrer en position (règle 1)
MIN_EXPECTED_GAIN_PCT = 0.05   # 5%

# Fenêtre maximale de résolution (règle 3)
MAX_DAYS_TO_RESOLUTION = 14

# Seuil de certitude pour clôture anticipée (règle 2) : YES ≤ 0.1% = NO gagne à 99.9%
EARLY_CLOSE_YES_THRESHOLD = 0.001


# ─── Helpers ────────────────────────────────────────────────────────────────

def calc_expected_gain_pct(win_rate: float, yes_price: float) -> float:
    """
    Calcule le gain attendu en % de la mise.

    Formule :
      gain si NO gagne = yes_price / (1 - yes_price) × (1 - frais)
      perte si YES gagne = 100% de la mise
      EV = win_rate × gain − (1 − win_rate) × 1.0
    """
    no_price = 1.0 - yes_price
    if no_price <= 0:
        return -1.0
    gain_if_win = (yes_price / no_price) * (1.0 - POLYMARKET_FEE)
    return win_rate * gain_if_win - (1.0 - win_rate) * 1.0


def parse_end_date(m: dict) -> datetime:
    """Extrait la date de résolution d'un marché, ou datetime 9999 si inconnue."""
    FAR_FUTURE = datetime(9999, 12, 31, tzinfo=timezone.utc)
    raw = m.get("endDate") or m.get("end_date_iso") or ""
    if not raw:
        return FAR_FUTURE
    try:
        raw = raw.rstrip("Z").replace("Z", "+00:00")
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
        else:
            dt = datetime.fromisoformat(raw + "T00:00:00")
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return FAR_FUTURE


# ─── Setup logs ─────────────────────────────────────────────────────────────

def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    logger.remove()
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_live_bot.log"
    logger.add(sys.stdout, colorize=True, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add(log_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")


# ─── Cycle principal ─────────────────────────────────────────────────────────

def run_once(dry_run: bool = False):
    """
    Un cycle complet :
      1. Tenter une clôture anticipée des positions à 99.9% de certitude
      2. Résoudre les positions dont le marché est clos
      3. Synchroniser le capital avec le vrai solde wallet
      4. Scanner les marchés S3 + SP (fenêtre 14 jours, gain ≥ 5%)
      5. Placer les ordres NO (ou simuler si dry_run=True)
    """
    portfolio = load_portfolio(PORTFOLIO_FILE)
    client    = None if dry_run else build_client()

    logger.info("=" * 60)
    logger.info(f"LIVE BOT S3+SP – {datetime.now().strftime('%Y-%m-%d %H:%M')} "
                f"{('[DRY-RUN]' if dry_run else '[REEL]')}")
    logger.info("=" * 60)

    # ── 1. Clôture anticipée à 99.9% de certitude (règle 2) ─────────────────
    # Si YES price ≤ 0.1%, notre pari NO est quasi certain.
    # On essaie de vendre les tokens NO maintenant pour libérer le capital
    # plus tôt, SAUF si la liquidité ne couvre pas le seuil de rentabilité.
    nb_early = 0
    if not dry_run and client:
        for mid in list(portfolio["positions_ouvertes"].keys()):
            pos    = portfolio["positions_ouvertes"][mid]
            market = get_market(mid)
            if market is None:
                continue
            current_yp = parse_yes_price(market)
            if current_yp is None or current_yp > EARLY_CLOSE_YES_THRESHOLD:
                continue  # pas encore à 99.9%

            # Calculer le nombre de tokens NO détenus
            entry_no_price = 1.0 - pos["entry_price_yes"]
            if entry_no_price <= 0:
                continue
            tokens_held = pos["bet_amount"] / entry_no_price

            # Prix minimum pour sortir sans P&L négatif (couvre les frais)
            breakeven_price = entry_no_price / (1.0 - POLYMARKET_FEE)

            no_token = get_no_token_id(market)
            if not no_token:
                continue

            # Vérifier la liquidité côté vente
            if not check_sell_liquidity(client, no_token, tokens_held, breakeven_price):
                logger.info(f"  [HOLD] {mid[:10]}... – YES={current_yp:.4f} mais liquidité "
                            f"insuffisante pour sortir sans P&L négatif, on attend la résolution")
                continue

            # Vendre les tokens NO
            resp = sell_no_position(client, no_token, tokens_held, breakeven_price)
            if resp:
                # Calculer le gain réalisé estimé
                sale_price = float(resp.get("price", 1.0 - current_yp))
                proceeds   = tokens_held * sale_price * (1.0 - POLYMARKET_FEE)
                profit     = round(proceeds - pos["bet_amount"], 2)
                # Fermer la position dans le portfolio (outcome NO gagne = 0)
                close_position(portfolio, mid, outcome=0, override_profit=profit)
                nb_early += 1
                logger.success(f"  [CLOTURE ANTICIPEE] {pos.get('question','')[:50]} | "
                               f"YES={current_yp:.4f} | profit={profit:+.2f}$")
            time.sleep(0.3)

    if nb_early:
        logger.info(f"Clôtures anticipées ce cycle : {nb_early}")

    # ── 2. Résoudre les positions dont le marché est clos ────────────────────
    nb_closed = 0
    for mid in list(portfolio["positions_ouvertes"].keys()):
        market  = get_market(mid)
        if market is None:
            continue
        outcome = parse_resolution(market)
        if outcome is None:
            continue
        close_position(portfolio, mid, outcome)
        nb_closed += 1
        time.sleep(0.1)
    logger.info(f"Positions résolues ce cycle : {nb_closed}")

    # ── 3. Synchroniser le capital avec le vrai solde wallet ─────────────────
    if not dry_run and client:
        real_balance = get_usdc_balance(client)
        if real_balance > 0:
            delta = round(real_balance - portfolio["capital_disponible"], 2)
            if delta > 1.0:
                portfolio["total_depose"] = round(
                    portfolio.get("total_depose", portfolio["capital_initial"]) + delta, 2
                )
                logger.info(f"Dépôt détecté : +{delta:.2f} USDC "
                            f"(total versé : {portfolio['total_depose']:.2f} USDC)")
            elif delta < -1.0:
                logger.warning(f"Retrait ou écart détecté : {delta:.2f} USDC")
            portfolio["capital_disponible"] = round(real_balance, 2)
            logger.info(f"Solde USDC wallet : {real_balance:.2f} USDC")

    # ── 4. Scanner les marchés S3 + SP ───────────────────────────────────────
    logger.info("Scan des marchés actifs (S3 + SP)...")
    markets = get_active_markets(min_volume=int(os.getenv("MIN_VOLUME_USD", "500")),
                                  max_pages=30)

    max_bet        = float(os.getenv("MAX_BET_USDC", "25"))
    nb_new         = 0
    skipped        = 0
    now_utc        = datetime.now(tz=timezone.utc)
    window_cutoff  = now_utc + timedelta(days=MAX_DAYS_TO_RESOLUTION)

    capital_commit = sum(p["bet_amount"] for p in portfolio["positions_ouvertes"].values())
    capital_kelly  = portfolio["capital_disponible"] + capital_commit
    logger.info(f"Capital Kelly : {capital_kelly:.2f}$ "
                f"(dispo {portfolio['capital_disponible']:.2f}$ + engagé {capital_commit:.2f}$)")

    # Collecter les signaux S3 + SP, filtrer et trier
    candidates = []
    skipped_14d = 0
    skipped_gain = 0

    for m in markets:
        yp = parse_yes_price(m)
        if not yp or not (0.05 <= yp <= 0.35):
            continue

        # Règle 3 : résolution dans les 14 jours
        end_dt = parse_end_date(m)
        if end_dt > window_cutoff:
            skipped_14d += 1
            continue

        sigs = check_signals(m, yp)
        for s in sigs:
            # Règle 1 : gain attendu ≥ 5%
            ev = calc_expected_gain_pct(s["win_rate_prior"], yp)
            if ev < MIN_EXPECTED_GAIN_PCT:
                skipped_gain += 1
                continue
            candidates.append((end_dt, -s["score"], m, s, yp, ev))

    # Résolution la plus proche d'abord, score le plus élevé en cas d'ex-æquo
    candidates.sort(key=lambda x: (x[0], x[1]))

    logger.info(f"Candidats : {len(candidates)} | Ignorés (>14j) : {skipped_14d} | "
                f"Ignorés (EV<5%) : {skipped_gain}")

    if not candidates and portfolio["capital_disponible"] > 1.0:
        logger.info("Aucun marché éligible dans la fenêtre 14 jours. "
                    "Capital conservé, on attend l'ouverture de nouveaux marchés.")

    # ── 5. Placer les ordres ─────────────────────────────────────────────────
    for end_dt, _neg_score, m, sig, yp, ev in candidates:
        mid      = str(m.get("id", ""))
        strategy = sig["strategy"]

        # Déjà en portefeuille (même marché, même stratégie)
        if any(p["market_id"] == mid and p["strategy"] == strategy
               for p in portfolio["positions_ouvertes"].values()):
            continue

        # Mise Kelly plafonnée
        bet = min(_kelly_size(sig["win_rate_prior"], yp, capital_kelly), max_bet)
        if bet <= 0 or bet > portfolio["capital_disponible"]:
            skipped += 1
            continue

        no_token = get_no_token_id(m)
        if no_token is None:
            logger.debug(f"Pas de token_id NO pour {mid[:10]}... – skipped")
            skipped += 1
            continue

        end_str = end_dt.strftime("%Y-%m-%d") if end_dt.year != 9999 else "???"

        if dry_run:
            logger.info(f"  [DRY-RUN] {strategy:3s} | {end_str} | "
                        f"{str(m.get('question',''))[:45]:45s} | "
                        f"YES={yp:.3f} | EV={ev*100:.1f}% | Mise={bet:.2f}$")
            add_position(portfolio, m, strategy, sig["win_rate_prior"], yp, sig["reason"],
                         bet_amount=bet)
            if mid in portfolio["positions_ouvertes"] and end_dt.year != 9999:
                portfolio["positions_ouvertes"][mid]["resolution_date"] = end_dt.strftime("%Y-%m-%d")
            nb_new += 1
        else:
            if not check_liquidity(client, no_token, bet):
                skipped += 1
                continue
            resp = place_no_order(client, no_token, bet, yp)
            if resp:
                m["_order_id"] = resp.get("orderID", "")
                add_position(portfolio, m, strategy, sig["win_rate_prior"], yp, sig["reason"],
                             bet_amount=bet)
                if mid in portfolio["positions_ouvertes"] and end_dt.year != 9999:
                    portfolio["positions_ouvertes"][mid]["resolution_date"] = end_dt.strftime("%Y-%m-%d")
                nb_new += 1
                logger.info(f"  [ENTREE] {strategy:3s} | {end_str} | "
                            f"{str(m.get('question',''))[:45]:45s} | "
                            f"YES={yp:.3f} | EV={ev*100:.1f}% | Mise={bet:.2f}$")
            else:
                skipped += 1
            time.sleep(0.3)

    logger.info(f"Nouvelles positions : {nb_new} | Skipped : {skipped}")

    save_portfolio(portfolio, PORTFOLIO_FILE)
    print_summary(portfolio)
    logger.success("Cycle terminé.")


# ─── Cleanup : ferme les positions hors règles résolues après le 31/05 ───────

def run_cleanup(dry_run: bool = False):
    """
    Ferme toutes les positions qui satisfont AU MOINS UNE condition :
      1. EV < 5% calculé sur le PRIX D'ENTRÉE
      2. Résolution après le 31/05/2026

    Tente de vendre les tokens NO sur le CLOB (min_price=0 pour forcer la vente).
    Si la vente échoue (pas de liquidité), signale la position pour clôture manuelle.
    """
    portfolio = load_portfolio(PORTFOLIO_FILE)
    client    = None if dry_run else build_client()
    cutoff    = datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc)

    logger.info("=" * 60)
    logger.info("CLEANUP – Fermeture des positions hors règles")
    logger.info("Critères : EV<5% (prix d'entrée) OU résolution>31/05")
    logger.info("=" * 60)

    to_close   = []
    manual_close = []   # positions sans liquidité à fermer manuellement

    for mid, pos in list(portfolio["positions_ouvertes"].items()):
        market = get_market(mid)
        if market is None:
            logger.warning(f"  Marché introuvable : {mid[:12]}...")
            continue

        # EV calculé sur le prix d'entrée (stratégie d'origine)
        ev     = calc_expected_gain_pct(pos["win_rate_prior"], pos["entry_price_yes"])
        end_dt = parse_end_date(market)

        reason = []
        if ev < MIN_EXPECTED_GAIN_PCT:
            reason.append(f"EV={ev*100:.1f}%<5%")
        if end_dt > cutoff:
            reason.append(f"résolution={end_dt.strftime('%Y-%m-%d')}>31/05")

        if reason:
            to_close.append((mid, pos, market, ev, end_dt, " | ".join(reason)))

    if not to_close:
        logger.info("Aucune position à fermer selon les critères de cleanup.")
        return

    logger.info(f"{len(to_close)} position(s) à fermer :")

    nb_sold = 0
    nb_failed = 0

    for mid, pos, market, ev, end_dt, reason_str in to_close:
        question = pos.get("question", "")[:60]
        end_str  = end_dt.strftime("%Y-%m-%d") if end_dt.year != 9999 else "???"
        logger.info(f"  {question}")
        logger.info(f"    Raison : {reason_str} | Résolution={end_str}")

        if dry_run:
            logger.info(f"    [DRY-RUN] Serait vendu")
            continue

        no_token = get_no_token_id(market)
        if not no_token:
            logger.warning(f"    Pas de token NO pour {mid[:12]}... → fermeture manuelle requise sur polymarket.com")
            nb_failed += 1
            continue

        entry_no_price = 1.0 - pos["entry_price_yes"]
        tokens_held    = pos["bet_amount"] / max(entry_no_price, 0.001)

        # Vendre au meilleur prix disponible (min_price=0 = accepter toute offre)
        resp = sell_no_position(client, no_token, tokens_held, min_price=0.0)
        if resp:
            sale_price = float(resp.get("price", entry_no_price))
            proceeds   = tokens_held * sale_price * (1.0 - POLYMARKET_FEE)
            profit     = round(proceeds - pos["bet_amount"], 2)
            close_position(portfolio, mid, outcome=0, override_profit=profit)
            nb_sold += 1
            logger.success(f"    Vendu – produit={proceeds:.2f}$ | P&L={profit:+.2f}$")
        else:
            nb_failed += 1
            logger.warning(f"    Impossible de vendre {mid[:12]}... – position conservée")

        time.sleep(0.5)

    save_portfolio(portfolio, PORTFOLIO_FILE)
    logger.info(f"Cleanup terminé : {nb_sold} fermées, {nb_failed} échecs.")
    if nb_failed:
        logger.warning(f"  {nb_failed} position(s) sans liquidité → fermer manuellement sur polymarket.com")
    logger.info("Le capital libéré sera redéployé au prochain cycle --loop.")


# ─── Commandes utilitaires ───────────────────────────────────────────────────

def cmd_create_keys():
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk or pk == "0x_VOTRE_CLE_PRIVEE_ICI":
        logger.error("Remplir POLYMARKET_PRIVATE_KEY dans .env avant de générer les clés")
        return
    logger.info("Génération des clés API Polymarket...")
    creds = create_api_keys(pk)
    logger.success("Clés générées – copier dans .env :")
    for k, v in creds.items():
        print(f"  POLYMARKET_{k.upper()}={v}")


def cmd_status():
    try:
        client  = build_client()
        balance = get_usdc_balance(client)
        logger.info(f"Solde USDC wallet : {balance:.2f} USDC")
    except Exception as e:
        logger.warning(f"Solde non disponible (clés API requises) : {e}")
    portfolio = load_portfolio(PORTFOLIO_FILE)
    print_summary(portfolio)


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    setup()
    parser = argparse.ArgumentParser(description="Live bot Polymarket S3+SP")
    parser.add_argument("--create-keys", action="store_true", help="Générer les clés API")
    parser.add_argument("--status",      action="store_true", help="Solde + positions")
    parser.add_argument("--dry-run",     action="store_true", help="Simuler sans ordres réels")
    parser.add_argument("--loop",        action="store_true", help="Boucle horaire + Notion 30min")
    parser.add_argument("--cleanup",     action="store_true",
                        help="Fermer positions hors règles résolvant après le 31/05/2026")
    args = parser.parse_args()

    if args.create_keys:
        cmd_create_keys()
        return

    if args.status:
        cmd_status()
        return

    if args.cleanup:
        run_cleanup(dry_run=args.dry_run)
        return

    if args.loop:
        logger.info("Mode boucle : cycle horaire + reporting Notion toutes les 30 min")
        while True:
            try:
                # Cycle de trading
                run_once(dry_run=args.dry_run)
                # Reporting Notion immédiat après le cycle
                portfolio = load_portfolio(PORTFOLIO_FILE)
                update_notion_report(portfolio)
            except Exception as e:
                logger.error(f"Erreur cycle : {e}")

            # Attendre 30 minutes, puis reporting Notion intermédiaire
            logger.info("Prochain reporting Notion dans 30 minutes, prochain cycle dans 60 minutes")
            time.sleep(1800)  # 30 min

            try:
                portfolio = load_portfolio(PORTFOLIO_FILE)
                update_notion_report(portfolio)
                logger.info("Reporting Notion 30min effectué")
            except Exception as e:
                logger.warning(f"Reporting Notion 30min échoué : {e}")

            time.sleep(1800)  # 30 min → total 60 min avant le prochain cycle
    else:
        run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
