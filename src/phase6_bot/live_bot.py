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

import sys, os, time, json, argparse, copy
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

from src.phase5_paper.polymarket_client import get_active_markets, get_active_event_markets, parse_yes_price, parse_resolution, get_market, get_market_by_token_id
from src.phase5_paper.strategy_signals   import check_signals, _is_crypto
from src.phase5_paper.paper_portfolio    import (
    load_portfolio, save_portfolio, add_position, close_position, print_summary,
)
from src.phase6_bot.order_executor import (
    get_no_token_id, get_yes_token_id, check_liquidity,
    place_no_order, place_yes_order, get_usdc_balance,
    check_sell_liquidity, sell_no_position, sell_yes_position,
    place_gtc_sell_no, place_gtc_sell_yes,
    get_all_clob_positions, get_total_portfolio_value,
)
from src.phase6_bot.notion_reporter import update_notion_report
from src.polymarket_common.client import build_directional_client, create_api_keys

PROJECT_ROOT   = Path(__file__).resolve().parents[2]
OUT_DIR        = PROJECT_ROOT / "outputs" / "phase6"
PORTFOLIO_FILE = OUT_DIR / "live_portfolio.json"
LOGS_DIR       = PROJECT_ROOT / "logs"

# Frais Polymarket : 2% du profit
POLYMARKET_FEE = 0.02

# Seuil de gain attendu minimum pour entrer en position (règle 1)
MIN_EXPECTED_GAIN_PCT = 0.05   # 5%

# Fenêtre maximale de résolution par stratégie
MAX_DAYS_S3 = 10   # S3 : résolution ≤ 10 jours
MAX_DAYS_SP = 10   # SP : résolution ≤ 10 jours
MAX_DAYS_TO_RESOLUTION = max(MAX_DAYS_S3, MAX_DAYS_SP)  # filtre global = 10j

# Seuil de certitude pour clôture anticipée (règle 2) : YES ≤ 1% = NO gagne à 99%
EARLY_CLOSE_YES_THRESHOLD = 0.01

# Stratégie SY : achat YES sur marchés très probables (94-98%) résolvant dans ≤ 96h
# Marchés à exclure : peuvent résoudre "other" ou présenter un risque de non-événement
_KW_OTHER_RISK = [
    # Primaires / nominations : la primaire peut être annulée, un candidat peut se retirer
    "primary", "primaries", "nomination", "nominate", "nominee",
    "qualify", "qualifier", "caucus",
    # Marchés multi-issue où un tiers peut gagner
    "plurality", "majority winner", "most votes",
    # Événements conditionnels incertains
    "if ", "assuming", "provided that",
]

def _has_other_outcome(market: dict) -> bool:
    """Retourne True si le marché peut résoudre à 'other' (non-binaire)."""
    outcomes_raw = market.get("outcomes") or "[]"
    try:
        import json
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        return any("other" in str(o).lower() for o in outcomes)
    except Exception:
        return False

def _is_other_risk(question: str) -> bool:
    """Retourne True si la question suggère un risque de résolution 'other'."""
    q = question.lower()
    return any(k in q for k in _KW_OTHER_RISK)

_KW_SPORT = [
    "football", "soccer", "basketball", "tennis", "nba", "nfl", "nhl", "mlb",
    "premier league", "ligue 1", "serie a", "bundesliga", "la liga",
    "champions league", "world cup", "copa ", "super bowl",
    "goal scorer", "top scorer", "top goal", "golden boot",
    "grand slam", "wimbledon", "formula 1", " f1 ",
    "ufc ", " boxing", "olympics", "rugby ", "cricket", " golf ",
    " fc ", "batting", "pitcher", "quarterback",
]

def _is_sport(q: str) -> bool:
    return any(k in q.lower() for k in _KW_SPORT)

SY_YES_MIN       = 0.92
SY_YES_MAX       = 0.98
SY_MAX_HOURS     = 96
MIN_EV_SY        = 0.01   # EV minimum 1% pour SY (marges plus faibles qu'en S3/SP)


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


def calc_expected_gain_pct_yes(win_rate: float, yes_price: float) -> float:
    """
    Calcule le gain attendu en % de la mise pour un achat YES.

    Formule :
      gain si YES gagne = (1 - yes_price) / yes_price × (1 - frais)
      perte si NO gagne = 100% de la mise
      EV = win_rate × gain − (1 − win_rate) × 1.0
    """
    if yes_price <= 0 or yes_price >= 1:
        return -1.0
    gain_if_win = (1.0 - yes_price) / yes_price * (1.0 - POLYMARKET_FEE)
    return win_rate * gain_if_win - (1.0 - win_rate) * 1.0


def parse_end_date(m: dict) -> datetime:
    """
    Extrait la date de résolution d'un marché.
    Si absente du champ endDate, tente de l'inférer depuis le titre
    (ex: "by May 26?" → 2026-05-26).
    Retourne datetime 9999 si vraiment inconnue.
    """
    import re
    FAR_FUTURE = datetime(9999, 12, 31, tzinfo=timezone.utc)

    raw = m.get("endDate") or m.get("endDateIso") or ""
    if raw:
        try:
            raw = raw.rstrip("Z").replace("Z", "+00:00")
            dt  = datetime.fromisoformat(raw if "T" in raw else raw + "T23:59:59")
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except (ValueError, TypeError):
            pass

    # Inférer depuis le titre : "by May 26", "by June 30", etc.
    question = m.get("question", "") or m.get("slug", "") or ""
    months = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
              "july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
    match = re.search(r"(?:by|through)\s+(\w+)\s+(\d{1,2})", question, re.IGNORECASE)
    if match:
        month_str = match.group(1).lower()
        day       = int(match.group(2))
        month     = months.get(month_str)
        if month:
            now  = datetime.now(tz=timezone.utc)
            year = now.year if (month >= now.month or (month == now.month and day >= now.day)) else now.year + 1
            try:
                return datetime(year, month, day, 23, 59, 59, tzinfo=timezone.utc)
            except ValueError:
                pass

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
    client    = None if dry_run else build_directional_client()

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

            # Seulement les positions NO (SP/S3) — pas les positions SY (achat YES)
            if pos.get("direction", "NO") != "NO":
                continue

            market = get_market(mid)
            if market is None:
                continue
            current_yp = parse_yes_price(market)
            if current_yp is None or current_yp > EARLY_CLOSE_YES_THRESHOLD:
                continue  # pas encore à 1%

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

            # Si NO > 0.99 (YES < 0.01), le CLOB refuse les ordres au-dessus de 0.99.
            # À ce stade la résolution est imminente — attendre la clôture naturelle.
            no_price_now = 1.0 - current_yp
            if no_price_now > 0.99:
                logger.info(f"  [HOLD NO] {mid[:10]}... – NO={no_price_now:.4f} > 0.99 CLOB max, "
                            f"résolution naturelle dans quelques heures")
                continue

            # Vérifier la liquidité côté vente.
            # Dès que YES ≤ seuil de clôture anticipée (1%), on force la vente
            # sans vérifier le carnet d'ordres (souvent vide sur marchés en fin de vie).
            # min_price=0 = accepter n'importe quel prix disponible.
            force_sell   = current_yp <= EARLY_CLOSE_YES_THRESHOLD   # YES ≤ 1% → forcer
            check_price  = 0.0 if force_sell else breakeven_price

            if not force_sell and not check_sell_liquidity(
                client, no_token, tokens_held, check_price
            ):
                logger.info(f"  [HOLD] {mid[:10]}... – YES={current_yp:.4f} mais liquidité "
                            f"insuffisante pour sortir sans P&L négatif, on attend la résolution")
                continue

            # Vendre les tokens NO
            resp = sell_no_position(client, no_token, tokens_held, check_price)
            if resp:
                sale_price = float(resp.get("price", 1.0 - current_yp))
                proceeds   = tokens_held * sale_price * (1.0 - POLYMARKET_FEE)
                profit     = round(proceeds - pos["bet_amount"], 2)
                close_position(portfolio, mid, outcome=0, override_profit=profit)
                nb_early += 1
                logger.success(f"  [CLOTURE ANTICIPEE NO] {pos.get('question','')[:50]} | "
                               f"YES={current_yp:.4f} | profit={profit:+.2f}$")
            time.sleep(0.3)

    # ── 1b. Clôture anticipée SY : YES ≥ 99¢ ───────────────────────────────────
    # Quand le prix YES atteint 99¢, l'événement est quasi certain.
    # On vend les tokens YES maintenant pour encaisser le gain et libérer le capital
    # plutôt que d'attendre la résolution (qui peut prendre encore 1-48h).
    EARLY_CLOSE_YES_SY_THRESHOLD = 0.99   # vendre quand YES ≥ 99¢

    if not dry_run and client:
        for mid in list(portfolio["positions_ouvertes"].keys()):
            pos = portfolio["positions_ouvertes"][mid]
            if pos.get("direction") != "YES" or pos.get("strategy") != "SY":
                continue   # seulement les positions SY (achat YES)

            market = get_market(mid)
            if market is None:
                continue
            current_yp = parse_yes_price(market)
            if current_yp is None or current_yp < EARLY_CLOSE_YES_SY_THRESHOLD:
                continue   # pas encore à 99¢

            # Calculer le nombre de tokens YES détenus
            entry_yes_price = pos["entry_price_yes"]
            if entry_yes_price <= 0:
                continue
            tokens_held = pos["bet_amount"] / entry_yes_price

            # Prix minimum pour sortir sans P&L négatif (couvre les frais)
            breakeven_price = entry_yes_price / (1.0 - POLYMARKET_FEE)

            yes_token = get_yes_token_id(market)
            if not yes_token:
                continue

            # Vérifier que la liquidité couvre la vente
            if not check_sell_liquidity(client, yes_token, tokens_held, breakeven_price):
                logger.info(f"  [HOLD SY] {mid[:10]}... – YES={current_yp:.4f} "
                            f"mais liquidité insuffisante, on attend la résolution")
                continue

            # Vendre les tokens YES
            resp = sell_yes_position(client, yes_token, tokens_held, breakeven_price)
            if resp:
                sale_price = float(resp.get("price", current_yp))
                proceeds   = tokens_held * sale_price * (1.0 - POLYMARKET_FEE)
                profit     = round(proceeds - pos["bet_amount"], 2)
                close_position(portfolio, mid, outcome=1, override_profit=profit)
                nb_early += 1
                logger.success(f"  [CLOTURE SY 99¢] {pos.get('question','')[:50]} | "
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
    # En dry-run on fait aussi la synchro (lecture seule) pour avoir le vrai capital.
    _sync_client = client if not dry_run else build_directional_client()
    try:
        real_balance = get_usdc_balance(_sync_client)
        portfolio["capital_disponible"] = round(real_balance, 2)
        logger.info(f"Solde USDC wallet : {real_balance:.2f} USDC")
        total = get_total_portfolio_value(_sync_client)
        if total > 0:
            portfolio["capital_total"] = total
    except Exception as e:
        logger.warning(f"Synchro capital impossible : {e}")
        if "capital_total" not in portfolio:
            portfolio["capital_total"] = portfolio["capital_disponible"]

    # ── 4. Scanner les marchés S3 + SP ───────────────────────────────────────
    logger.info("Scan des marchés actifs (S3 + SP)...")
    min_vol = int(os.getenv("MIN_VOLUME_USD", "500"))

    # Source 1 : endpoint /markets (marchés standalone)
    markets = get_active_markets(min_volume=min_vol, max_pages=30)

    # Source 2 : endpoint /events (marchés neg-risk groupés, ex: Iran ceasefire)
    # Fusionnés par ID pour éviter les doublons
    event_markets = get_active_event_markets(min_volume=min_vol)
    # Index par id pour pouvoir injecter _event_id sur les marchés déjà présents
    markets_by_id = {str(m.get("id", "")): m for m in markets}
    existing_ids  = set(markets_by_id.keys())
    added = 0
    injected = 0
    for m in event_markets:
        mid = str(m.get("id", ""))
        if not mid:
            continue
        if mid not in existing_ids:
            markets.append(m)
            existing_ids.add(mid)
            added += 1
        elif "_event_id" in m and "_event_id" not in markets_by_id[mid]:
            # Marché déjà présent via /markets : injecter l'event_id pour
            # que les gardes anti-doublon fonctionnent même pour ces marchés
            markets_by_id[mid]["_event_id"] = m["_event_id"]
            injected += 1
    if added:
        logger.info(f"+ {added} marchés supplémentaires via /events (neg-risk)")
    if injected:
        logger.debug(f"+ {injected} marchés enrichis avec _event_id depuis /events")

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
    skipped_14d   = 0
    skipped_gain  = 0
    skipped_range = 0   # hors plage de prix (YES < 5% ou 35-92% ou > 98%)
    sy_cutoff = now_utc + timedelta(hours=SY_MAX_HOURS)

    for m in markets:
        yp = parse_yes_price(m)
        if not yp:
            continue

        # Exclure les marchés pouvant résoudre "other" ou à risque de non-événement.
        # Exception : marchés neg-risk avec _event_id — le "Other" dans les outcomes
        # désigne "un autre candidat gagne", ce qui est bien géré par le YES/NO du
        # sous-marché. Ces marchés ne risquent pas la résolution "other" au sens
        # d'un événement non-binaire.
        q_raw     = str(m.get("question", ""))
        is_neg_risk = bool(m.get("_event_id"))
        if _is_other_risk(q_raw):
            continue
        if not is_neg_risk and _has_other_outcome(m):
            continue

        in_sp_range = 0.05 <= yp <= 0.35
        in_sy_range = SY_YES_MIN <= yp <= SY_YES_MAX

        if not in_sp_range and not in_sy_range:
            skipped_range += 1
            continue

        end_dt = parse_end_date(m)

        # ── Stratégie SY : achat YES, fenêtre ≤ 96h ────────────────────────
        if in_sy_range:
            if end_dt < now_utc or end_dt > sy_cutoff:
                skipped_14d += 1
                continue
            q = str(m.get("question", "")).lower()
            if _is_crypto(q) or _is_sport(q):
                continue
            # Win rate estimé = prix marché + 1.5% (hypothèse d'edge court terme)
            win_rate_sy = min(yp + 0.015, 0.995)
            ev_sy = calc_expected_gain_pct_yes(win_rate_sy, yp)
            if ev_sy < MIN_EV_SY:
                skipped_gain += 1
                continue
            cat_prio = _category_priority(str(m.get("question", "")).lower())
            sig_sy = {
                "strategy":       "SY",
                "win_rate_prior": win_rate_sy,
                "score":          1.0,
                "reason":         f"YES={yp:.3f} in 94-98%, résolution ≤96h",
            }
            candidates.append((cat_prio, 1, end_dt, -1.0, m, sig_sy, yp, ev_sy))
            continue   # pas de signal S3/SP sur le même marché

        # ── Stratégies S3/SP : achat NO, fenêtre ≤ 12j ──────────────────────
        # end_dt < now_utc = marché expiré non résolu (overdue) → skip
        # end_dt > window_cutoff = résolution trop lointaine → skip
        if end_dt < now_utc or end_dt > window_cutoff:
            skipped_14d += 1
            continue

        sigs = check_signals(m, yp)
        # S3 prioritaire : si S3 se déclenche, ignorer SP sur le même marché
        s3 = [s for s in sigs if s["strategy"] == "S3"]
        best_sigs = s3 if s3 else [s for s in sigs if s["strategy"] == "SP"]
        for s in best_sigs:
            # Fenêtre max par stratégie
            max_days = MAX_DAYS_S3 if s["strategy"] == "S3" else MAX_DAYS_SP
            if end_dt > now_utc + timedelta(days=max_days):
                skipped_14d += 1
                continue
            # Règle 1 : gain attendu ≥ 5%
            ev = calc_expected_gain_pct(s["win_rate_prior"], yp)
            if ev < MIN_EXPECTED_GAIN_PCT:
                skipped_gain += 1
                continue
            cat_prio      = _category_priority(str(m.get("question", "")).lower())
            strategy_prio = 0 if s["strategy"] == "S3" else 2  # S3=0, SP=2 (SY=1 entre les deux)
            candidates.append((cat_prio, strategy_prio, end_dt, -s["score"], m, s, yp, ev))

    # Tri : 1) résolution la plus proche  2) S3(0) > SY(1) > SP(2)  3) catégorie  4) score
    candidates.sort(key=lambda x: (x[2], x[1], x[0], x[3]))

    logger.info(f"Candidats : {len(candidates)} | Ignorés (>{MAX_DAYS_TO_RESOLUTION}j) : {skipped_14d} | "
                f"Ignorés (EV<5%) : {skipped_gain} | Ignorés (hors plage prix) : {skipped_range}")

    if not candidates and portfolio["capital_disponible"] > 1.0:
        logger.info(f"Aucun marché éligible dans la fenêtre {MAX_DAYS_TO_RESOLUTION} jours. "
                    "Capital conservé, on attend l'ouverture de nouveaux marchés.")

    # ── 5. Placer les ordres ─────────────────────────────────────────────────
    # En dry-run : prendre un snapshot du portfolio AVANT d'ajouter des positions
    # pour pouvoir sauvegarder l'état réel (capital synchro uniquement) en fin de cycle.
    # Sans ça, chaque dry-run pollue le JSON avec de fausses positions qui bloquent
    # les vrais candidats lors du cycle suivant.
    _portfolio_snapshot = copy.deepcopy(portfolio) if dry_run else None

    for _cat_prio, _strategy_prio, end_dt, _neg_score, m, sig, yp, ev in candidates:
        mid      = str(m.get("id", ""))
        strategy = sig["strategy"]
        logger.info(f"  → {strategy} | YES={yp:.3f} | end={end_dt.strftime('%m-%d')} | "
                    f"{str(m.get('question',''))[:50]}")

        # Déjà en portefeuille sur CE marché exact
        if any(p["market_id"] == mid
               for p in portfolio["positions_ouvertes"].values()):
            logger.info(f"  [SKIP] {str(m.get('question',''))[:50]} — déjà en portefeuille")
            continue

        # Même événement parent (neg-risk / primaires / multi-candidats) :
        # Garde 1 : conditionId identique (marchés partageant le même contrat CTF)
        cond_id = str(m.get("conditionId") or m.get("condition_id") or "").strip()
        if cond_id and any(
            str(p.get("condition_id", "")) == cond_id
            for p in portfolio["positions_ouvertes"].values()
        ):
            logger.info(f"  [SKIP] {str(m.get('question',''))[:50]} "
                        f"— conditionId déjà en portefeuille ({cond_id[:12]}...)")
            continue

        # Garde 2 : event_id identique (élections multi-candidats)
        # Règles :
        #   a) SY bloqué si S3/SP déjà en portefeuille sur le même événement
        #      → empêche de doubler la mise directionnelle sur une même élection
        #   b) S3/SP bloqué si SY déjà en portefeuille sur le même événement
        #      → idem (les deux sont corrélés : tous deux perdent si le favori perd)
        #   c) S3/SP vs S3/SP sur le même événement : l'un gagne toujours, l'autre perd
        #   d) SY vs SY : idem
        event_id = str(m.get("_event_id", "")).strip()
        if event_id:
            conflict = next(
                (p for p in portfolio["positions_ouvertes"].values()
                 if str(p.get("event_id", "")) == event_id),
                None
            )
            if conflict:
                logger.info(
                    f"  [SKIP {strategy}] {str(m.get('question',''))[:50]} "
                    f"— événement Gamma déjà en portefeuille via {conflict.get('strategy','?')} "
                    f"sur \"{conflict.get('question','')[:35]}\" (event_id={event_id[:12]}...)"
                )
                continue

        # Mise = min(5% × capital total, 200$, USDC disponible - 5$ de réserve)
        # Pas de seuil minimum : tout le capital restant est déployé même si
        # inférieur à la Kelly cible, tant que ≥ 1$ (min CLOB).
        usdc_utilisable = portfolio["capital_disponible"] - 5.0
        kelly_target    = min(capital_kelly * 0.05, max_bet)
        bet = min(kelly_target, usdc_utilisable)
        if bet < 1.0:
            skipped += 1
            continue

        end_str   = end_dt.strftime("%Y-%m-%d %H:%M") if end_dt.year != 9999 else "???"
        is_sy     = (strategy == "SY")
        direction = "YES" if is_sy else "NO"

        if is_sy:
            token = get_yes_token_id(m)
        else:
            token = get_no_token_id(m)

        if token is None:
            logger.debug(f"Pas de token_id {direction} pour {mid[:10]}... – skipped")
            skipped += 1
            continue

        if dry_run:
            logger.info(f"  [DRY-RUN] {strategy:3s} | {end_str} | "
                        f"{str(m.get('question',''))[:45]:45s} | "
                        f"YES={yp:.3f} | EV={ev*100:.1f}% | Mise={bet:.2f}$ | dir={direction}")
            add_position(portfolio, m, strategy, sig["win_rate_prior"], yp, sig["reason"],
                         bet_amount=bet, direction=direction)
            if mid in portfolio["positions_ouvertes"] and end_dt.year != 9999:
                portfolio["positions_ouvertes"][mid]["resolution_date"] = end_dt.strftime("%Y-%m-%d")
            nb_new += 1
        else:
            liq_ok, best_ask = check_liquidity(client, token, bet)
            if not liq_ok:
                skipped += 1
                continue
            resp = (place_yes_order(client, token, bet, yp, clob_ask_price=best_ask)
                    if is_sy else
                    place_no_order(client, token, bet, yp, clob_ask_price=best_ask))
            if resp:
                actual_bet = resp.get("_filled_usdc", bet)
                m["_order_id"] = resp.get("orderID", "")
                add_position(portfolio, m, strategy, sig["win_rate_prior"], yp, sig["reason"],
                             bet_amount=actual_bet, direction=direction)
                if mid in portfolio["positions_ouvertes"] and end_dt.year != 9999:
                    portfolio["positions_ouvertes"][mid]["resolution_date"] = end_dt.strftime("%Y-%m-%d")
                nb_new += 1
                logger.info(f"  [ENTREE] {strategy:3s} | {end_str} | "
                            f"{str(m.get('question',''))[:45]:45s} | "
                            f"YES={yp:.3f} | EV={ev*100:.1f}% | Mise={actual_bet:.2f}$ | dir={direction}")

                # Ordre GTC immédiat : vendre automatiquement quand NO atteint 99¢ (ou YES 99¢).
                # Set-and-forget : le CLOB Polymarket l'exécute sans que le bot n'ait à tourner.
                time.sleep(0.2)
                if is_sy:
                    # SY : vendre tokens YES quand YES = 99¢
                    tokens_gtc = actual_bet / yp if yp > 0 else 0
                    if tokens_gtc > 0.01:
                        place_gtc_sell_yes(client, token, tokens_gtc, limit_price=0.99)
                else:
                    # S3/SP : vendre tokens NO quand NO = 99¢ (= YES ≤ 1¢)
                    no_price_entry = 1.0 - yp
                    tokens_gtc = actual_bet / no_price_entry if no_price_entry > 0 else 0
                    if tokens_gtc > 0.01:
                        place_gtc_sell_no(client, token, tokens_gtc, limit_price=0.99)
            else:
                skipped += 1
            time.sleep(0.3)

    logger.info(f"Nouvelles positions : {nb_new} | Skipped : {skipped}")

    # Re-synchro finale : corrige les écarts dus aux FAK partiels ou timing
    if not dry_run and client:
        real_balance = get_usdc_balance(client)
        if real_balance > 0:
            portfolio["capital_disponible"] = round(real_balance, 2)

    # En dry-run : sauvegarder le snapshot (capital synchro, sans les fausses positions).
    # En cycle réel : sauvegarder le portfolio complet avec les nouvelles positions.
    if dry_run and _portfolio_snapshot is not None:
        save_portfolio(_portfolio_snapshot, PORTFOLIO_FILE)
    else:
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
    client    = None if dry_run else build_directional_client()
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
    funder = os.environ.get("POLYMARKET_PROXY_WALLET", "").strip() or None
    if not funder:
        logger.error("Remplir POLYMARKET_PROXY_WALLET dans .env avant de générer les clés")
        return
    logger.info("Génération des clés API Polymarket...")
    # signature_type=2 (GNOSIS_SAFE) + funder=POLYMARKET_PROXY_WALLET : forcés
    # explicitement pour générer des credentials liés au wallet proxy du bot
    # directionnel, et non à un EOA par défaut.
    creds = create_api_keys(pk, signature_type=2, funder=funder)
    logger.success("Clés générées – copier dans .env :")
    for k, v in creds.items():
        print(f"  POLYMARKET_{k.upper()}={v}")


def cmd_status():
    portfolio = load_portfolio(PORTFOLIO_FILE)
    try:
        client  = build_directional_client()
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
