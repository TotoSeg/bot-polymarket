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

from src.phase5_paper.polymarket_client import get_active_markets, parse_yes_price, parse_resolution, get_market, get_market_by_token_id
from src.phase5_paper.strategy_signals   import check_signals
from src.phase5_paper.paper_portfolio    import (
    load_portfolio, save_portfolio, add_position, close_position, print_summary,
)
from src.phase6_bot.order_executor import (
    build_client, create_api_keys, get_no_token_id, check_liquidity,
    place_no_order, get_usdc_balance,
    check_sell_liquidity, sell_no_position, get_all_clob_positions,
    get_total_portfolio_value,
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
MAX_DAYS_TO_RESOLUTION = 12

# Seuil de certitude pour clôture anticipée (règle 2) : YES ≤ 1% = NO gagne à 99%
EARLY_CLOSE_YES_THRESHOLD = 0.01


# Priorité des catégories (0 = plus prioritaire)
_CATEGORY_PRIORITY = [
    (0, ["trump", "biden", "harris", "congress", "senate", "democrat", "republican",
         "white house", "governor", "parliament", "prime minister", "chancellor",
         "president", "political", "secretary of state", "cabinet", "minister",
         "veto", "impeach", "approval rating", "supreme court", "judiciary",
         "legislation", "policy ", "administration"]),                     # politique
    (1, ["ceasefire", "nato", "sanction", "coup", "invasion", "war", "treaty",
         "nuclear", "troops", "diplomacy", "missile", "military", "conflict",
         "peace deal", "peace talks", "alliance", "embargo", "airstrike",
         "territory", "occupation", "united nations", "un security",
         "regime", "sovereignty", "geopolit"]),                            # géopolitique
    (2, ["iran", "tehran", "iranian", "ayatollah", "khamenei", "irgc",
         "persian", "nuclear deal", "jcpoa", "isfahan"]),                  # iran
    (3, ["election", "vote", "ballot", "referendum", "primary", "caucus",
         "polling", "constituency", "runoff", "candidate", "nominee",
         "midterm", "recount", "swing state", "turnout", "electoral"]),    # élection
    (4, ["music", "movie", "film", "award", "oscar", "grammy", "celebrity",
         "artist", "singer", "actor", "album", "song", "tv show",
         "television", "box office", "streaming", "netflix", "spotify",
         "billboard", "emmy", "bafta", "golden globe", "viral"]),         # culture
]


def _category_priority(question_lc: str) -> int:
    """Retourne la priorité catégorie (0=politique … 4=culture, 5=autre)."""
    for priority, keywords in _CATEGORY_PRIORITY:
        if any(k in question_lc for k in keywords):
            return priority
    return 5


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
    raw = m.get("endDate") or m.get("endDateIso") or ""
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
    # capital_disponible = solde USDC réel. C'est toujours la source de vérité.
    # capital_total      = USDC + valeur des positions (base du calcul Kelly).
    if not dry_run and client:
        real_balance = get_usdc_balance(client)
        portfolio["capital_disponible"] = round(real_balance, 2)
        logger.info(f"Solde USDC wallet : {real_balance:.2f} USDC")
        total = get_total_portfolio_value(client)
        if total > 0:
            portfolio["capital_total"] = total
    elif dry_run:
        # En dry-run : utiliser capital_total stocké si disponible, sinon disponible seul
        if "capital_total" not in portfolio:
            portfolio["capital_total"] = portfolio["capital_disponible"]

    # ── 4. Scanner les marchés S3 + SP ───────────────────────────────────────
    logger.info("Scan des marchés actifs (S3 + SP)...")
    markets = get_active_markets(min_volume=int(os.getenv("MIN_VOLUME_USD", "500")),
                                  max_pages=30)

    max_bet        = float(os.getenv("MAX_BET_USDC", "200"))
    nb_new         = 0
    skipped        = 0
    now_utc        = datetime.now(tz=timezone.utc)
    window_cutoff  = now_utc + timedelta(days=MAX_DAYS_TO_RESOLUTION)

    # Capital Kelly = valeur totale du portefeuille (USDC + positions).
    # Priorité : capital_total (synchro depuis Polymarket) > variable d'env > capital_disponible.
    capital_kelly = (
        portfolio.get("capital_total")
        or float(os.getenv("KELLY_CAPITAL_OVERRIDE", "0"))
        or portfolio["capital_disponible"]
    )
    logger.info(f"Capital Kelly : {capital_kelly:.2f}$  "
                f"(dispo {portfolio['capital_disponible']:.2f}$)")

    # Collecter les signaux S3 + SP, filtrer et trier
    candidates = []
    skipped_14d = 0
    skipped_gain = 0

    for m in markets:
        yp = parse_yes_price(m)
        if not yp or not (0.05 <= yp <= 0.35):
            continue

        # Règle 3 : résolution dans la fenêtre [maintenant, +12j]
        # end_dt < now_utc = marché expiré non résolu (overdue) → skip
        # end_dt > window_cutoff = résolution trop lointaine → skip
        end_dt = parse_end_date(m)
        if end_dt < now_utc or end_dt > window_cutoff:
            skipped_14d += 1
            continue

        sigs = check_signals(m, yp)
        # S3 prioritaire : si S3 se déclenche, ignorer SP sur le même marché
        s3 = [s for s in sigs if s["strategy"] == "S3"]
        best_sigs = s3 if s3 else [s for s in sigs if s["strategy"] == "SP"]
        for s in best_sigs:
            # Règle 1 : gain attendu ≥ 5%
            ev = calc_expected_gain_pct(s["win_rate_prior"], yp)
            if ev < MIN_EXPECTED_GAIN_PCT:
                skipped_gain += 1
                continue
            cat_prio      = _category_priority(str(m.get("question", "")).lower())
            strategy_prio = 0 if s["strategy"] == "S3" else 1  # S3 avant SP
            candidates.append((cat_prio, strategy_prio, end_dt, -s["score"], m, s, yp, ev))

    # Tri : 1) priorité catégorie  2) S3 avant SP  3) résolution la plus proche  4) score décroissant
    candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3]))

    logger.info(f"Candidats : {len(candidates)} | Ignorés (>{MAX_DAYS_TO_RESOLUTION}j) : {skipped_14d} | "
                f"Ignorés (EV<5%) : {skipped_gain}")

    if not candidates and portfolio["capital_disponible"] > 1.0:
        logger.info(f"Aucun marché éligible dans la fenêtre {MAX_DAYS_TO_RESOLUTION} jours. "
                    "Capital conservé, on attend l'ouverture de nouveaux marchés.")

    # ── 5. Placer les ordres ─────────────────────────────────────────────────
    for _cat_prio, _strategy_prio, end_dt, _neg_score, m, sig, yp, ev in candidates:
        mid      = str(m.get("id", ""))
        strategy = sig["strategy"]

        # Déjà en portefeuille (une seule position par marché, S3 prioritaire)
        if any(p["market_id"] == mid
               for p in portfolio["positions_ouvertes"].values()):
            continue

        # Mise = min(5% × capital total, 200$, USDC disponible)
        bet = min(capital_kelly * 0.05, max_bet, portfolio["capital_disponible"])
        if bet < 1.0:
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
                actual_bet = resp.get("_filled_usdc", bet)  # montant réellement exécuté (FAK partiel)
                m["_order_id"] = resp.get("orderID", "")
                add_position(portfolio, m, strategy, sig["win_rate_prior"], yp, sig["reason"],
                             bet_amount=actual_bet)
                if mid in portfolio["positions_ouvertes"] and end_dt.year != 9999:
                    portfolio["positions_ouvertes"][mid]["resolution_date"] = end_dt.strftime("%Y-%m-%d")
                nb_new += 1
                logger.info(f"  [ENTREE] {strategy:3s} | {end_str} | "
                            f"{str(m.get('question',''))[:45]:45s} | "
                            f"YES={yp:.3f} | EV={ev*100:.1f}% | Mise={actual_bet:.2f}$")
            else:
                skipped += 1
            time.sleep(0.3)

    logger.info(f"Nouvelles positions : {nb_new} | Skipped : {skipped}")

    # Re-synchro finale : corrige les écarts dus aux FAK partiels ou timing
    if not dry_run and client:
        real_balance = get_usdc_balance(client)
        if real_balance > 0:
            portfolio["capital_disponible"] = round(real_balance, 2)

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
    now_utc   = datetime.now(tz=timezone.utc)

    logger.info("=" * 60)
    logger.info("CLEANUP – Fermeture des positions hors règles")
    logger.info("Critères : EV<5% OU résolution>31/05 OU marché en retard (endDate dépassée)")
    logger.info("=" * 60)

    to_close = []

    def _scan_position(mid, pos, market, source="portfolio"):
        """Évalue si une position doit être fermée et l'ajoute à to_close si oui."""
        question = pos.get("question", "?")[:60]
        ev       = calc_expected_gain_pct(pos.get("win_rate_prior", 0.97),
                                          pos.get("entry_price_yes", 0.07))

        if market is not None:
            end_dt = parse_end_date(market)
        else:
            stored = pos.get("resolution_date", "")
            if stored:
                try:
                    end_dt = datetime.fromisoformat(stored + "T00:00:00").replace(tzinfo=timezone.utc)
                except ValueError:
                    end_dt = datetime(9999, 12, 31, tzinfo=timezone.utc)
            else:
                end_dt = datetime(9999, 12, 31, tzinfo=timezone.utc)

        end_str = end_dt.strftime("%Y-%m-%d") if end_dt.year != 9999 else "inconnue"
        logger.info(f"  [{source}] {question[:45]} | EV={ev*100:.1f}% | résolution={end_str}")

        reason = []
        if ev < MIN_EXPECTED_GAIN_PCT:
            reason.append(f"EV={ev*100:.1f}%<5%")
        if end_dt > cutoff:
            reason.append(f"résolution={end_str}>31/05")
        # Marché dont l'endDate est dépassée depuis plus d'un jour mais toujours ouvert
        if end_dt < now_utc - timedelta(days=1):
            reason.append(f"marché en retard (endDate={end_str} dépassée)")

        if reason:
            to_close.append((mid, pos, market, end_dt, " | ".join(reason)))

    # ── Scan 1 : positions du portfolio JSON ─────────────────────────────────
    logger.info("Scan des positions du portfolio JSON...")
    for mid, pos in list(portfolio["positions_ouvertes"].items()):
        market = get_market(mid)
        _scan_position(mid, pos, market, source="JSON")
        time.sleep(0.1)

    # ── Scan 2 : positions CLOB non trackées dans le portfolio JSON ──────────
    logger.info("Scan des positions CLOB (wallet Polymarket)...")
    clob_positions = get_all_clob_positions()
    known_tokens   = {
        p.get("token_id", "") for p in portfolio["positions_ouvertes"].values()
    }

    for clob_pos in clob_positions:
        token_id = str(clob_pos.get("asset_id") or clob_pos.get("token_id") or "")
        balance  = float(clob_pos.get("balance", 0) or 0)
        outcome  = str(clob_pos.get("outcome", "")).upper()

        if not token_id or balance < 0.001:
            continue
        if token_id in known_tokens:
            continue   # déjà scanné via le portfolio JSON
        if outcome == "YES":
            continue   # on ne détient que des NO dans nos stratégies

        # Retrouver le marché associé à ce token
        market = get_market_by_token_id(token_id)
        if market is None:
            logger.warning(f"  [CLOB] token {token_id[:12]}... — marché introuvable, fermeture manuelle requise")
            continue

        # Reconstruire un pseudo-pos pour l'évaluation
        yes_p = parse_yes_price(market) or 0.07
        pos = {
            "question":        str(market.get("question", ""))[:80],
            "strategy":        "??",
            "win_rate_prior":  0.97,
            "entry_price_yes": yes_p,
            "bet_amount":      balance * (1.0 - yes_p),   # estimation USDC investi
            "token_id":        token_id,
            "tokens_held":     balance,
        }
        _scan_position(token_id, pos, market, source="CLOB hors JSON")
        time.sleep(0.1)

    if not to_close:
        logger.info("Aucune position à fermer selon les critères de cleanup.")
        return

    logger.info(f"{len(to_close)} position(s) à fermer :")

    nb_sold = 0
    nb_failed = 0

    for mid, pos, market, end_dt, reason_str in to_close:
        question = pos.get("question", "")[:60]
        end_str  = end_dt.strftime("%Y-%m-%d") if end_dt.year != 9999 else "???"
        logger.info(f"  {question}")
        logger.info(f"    Raison : {reason_str} | Résolution={end_str}")

        if dry_run:
            logger.info(f"    [DRY-RUN] Serait vendu")
            continue

        # Résoudre le token NO : depuis le market si dispo, ou depuis pos (CLOB hors JSON)
        if market is not None:
            no_token = get_no_token_id(market)
        else:
            no_token = pos.get("token_id")

        if not no_token:
            logger.warning(f"    Pas de token NO pour {mid[:12]}... → fermeture manuelle sur polymarket.com")
            nb_failed += 1
            continue

        # Nombre de tokens : depuis pos["tokens_held"] (CLOB hors JSON) ou calcul
        tokens_held = pos.get("tokens_held") or (
            pos["bet_amount"] / max(1.0 - pos["entry_price_yes"], 0.001)
        )

        resp = sell_no_position(client, no_token, tokens_held, min_price=0.0)
        if resp:
            sale_price = float(resp.get("price", 1.0 - pos["entry_price_yes"]))
            proceeds   = tokens_held * sale_price * (1.0 - POLYMARKET_FEE)
            profit     = round(proceeds - pos["bet_amount"], 2)
            # Retirer du portfolio JSON si la position y était trackée
            if mid in portfolio["positions_ouvertes"]:
                close_position(portfolio, mid, outcome=0, override_profit=profit)
            nb_sold += 1
            logger.success(f"    Vendu – produit={proceeds:.2f}$ | P&L={profit:+.2f}$")
        else:
            nb_failed += 1
            logger.warning(f"    Impossible de vendre → fermeture manuelle sur polymarket.com")

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
    portfolio = load_portfolio(PORTFOLIO_FILE)
    try:
        client  = build_client()
        balance = get_usdc_balance(client)
        portfolio["capital_disponible"] = round(balance, 2)
        logger.info(f"Solde USDC wallet : {balance:.2f} USDC")
        total = get_total_portfolio_value(client)
        if total > 0:
            portfolio["capital_total"] = total
            logger.info(f"Capital Kelly (total) : {total:.2f}$")
        save_portfolio(portfolio, PORTFOLIO_FILE)
    except Exception as e:
        logger.warning(f"Solde non disponible (clés API requises) : {e}")
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
