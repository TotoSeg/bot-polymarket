"""
Phase 5 — Gestion du portefeuille paper trading
=================================================
Persiste l'état du portefeuille dans un fichier JSON entre les runs.

Structure du fichier portfolio.json :
{
  "capital_initial": 1000.0,
  "capital_disponible": 987.50,
  "positions_ouvertes": {
    "<market_id>": {
      "market_id":      "0xabc...",
      "question":       "Will X happen?",
      "strategy":       "S2",
      "win_rate_prior": 0.999,
      "entry_price_yes": 0.03,
      "bet_amount":     10.0,
      "entry_date":     "2026-04-28T10:00:00",
      "reason":         "YES=0.03 < 5%"
    }
  },
  "trades_clos": [
    {
      ...même champs...,
      "exit_date":  "2026-05-01T12:00:00",
      "outcome":    0,           (0=NO gagne, 1=YES gagne)
      "profit_net": 7.35,
      "win":        true
    }
  ]
}
"""

import sys
import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Frais Polymarket : 2% du profit
POLYMARKET_FEE = 0.02

# Fraction Kelly et borne max
KELLY_FRACTION = 0.25
MAX_BET_PCT    = 0.05   # 5% du capital initial max par trade (↓ de 10%)
                         # → permet ~20 positions simultanées au lieu de 10

# Nb max de positions par stratégie (évite sur-concentration)
MAX_POSITIONS_PER_STRATEGY = {
    "S1": 10, "S2": 30, "S3": 15, "S4": 10,
    "S5": 5,  "S6": 10, "BTC": 10,
}


def _kelly_size(win_rate: float, p_yes: float, initial_capital: float) -> float:
    """Calcule la mise en dollars selon le Kelly criterion fractionné."""
    p_no   = 1.0 - p_yes
    if p_no <= 0:
        return 0.0
    odds_b = p_yes / p_no   # gain/perte ratio pour un pari NO
    if odds_b <= 0 or win_rate <= 0:
        return 0.0

    q      = 1.0 - win_rate
    f_star = (odds_b * win_rate - q) / odds_b
    f_kelly = f_star * KELLY_FRACTION
    f_kelly = max(0.001, min(f_kelly, MAX_BET_PCT))  # clipper

    # Plafonner à initial_capital * MAX_BET_PCT pour éviter l'exponentiel
    return round(initial_capital * f_kelly, 2)


def load_portfolio(path: Path) -> dict:
    """Charge le portefeuille depuis le fichier JSON (crée un neuf si absent)."""
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    # Nouveau portefeuille vierge
    return {
        "capital_initial":    1000.0,
        "capital_disponible": 1000.0,
        "positions_ouvertes": {},
        "trades_clos":        [],
    }


def save_portfolio(portfolio: dict, path: Path):
    """Sauvegarde le portefeuille dans le fichier JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(portfolio, f, indent=2, ensure_ascii=False)


def add_position(portfolio: dict, market: dict, strategy: str,
                 win_rate_prior: float, entry_price_yes: float, reason: str):
    """
    Enregistre une nouvelle position ouverte.

    Retourne True si la position a été ajoutée, False si déjà présente.
    """
    market_id = str(market.get("id", ""))
    if market_id in portfolio["positions_ouvertes"]:
        return False  # Déjà en portefeuille

    # Vérifier la limite par stratégie
    nb_open_strat = sum(
        1 for p in portfolio["positions_ouvertes"].values()
        if p["strategy"] == strategy
    )
    max_strat = MAX_POSITIONS_PER_STRATEGY.get(strategy, 10)
    if nb_open_strat >= max_strat:
        logger.debug(f"Limite {strategy} atteinte ({nb_open_strat}/{max_strat})")
        return False

    initial_capital = portfolio["capital_initial"]
    bet_amount = _kelly_size(win_rate_prior, entry_price_yes, initial_capital)

    # Ne pas ouvrir si capital insuffisant
    if bet_amount > portfolio["capital_disponible"]:
        logger.debug(f"Capital insuffisant pour {market_id[:10]}... ({bet_amount}$ > dispo)")
        return False

    portfolio["capital_disponible"] -= bet_amount
    portfolio["positions_ouvertes"][market_id] = {
        "market_id":        market_id,
        "question":         str(market.get("question", ""))[:80],
        "strategy":         strategy,
        "win_rate_prior":   win_rate_prior,
        "entry_price_yes":  round(entry_price_yes, 4),
        "bet_amount":       bet_amount,
        "entry_date":       datetime.now(tz=timezone.utc).isoformat(),
        "reason":           reason,
    }
    logger.success(
        f"  [ENTREE] {strategy:3s} | {str(market.get('question',''))[:50]:50s} | "
        f"YES={entry_price_yes:.3f} | Mise={bet_amount:.2f}$"
    )
    return True


def close_position(portfolio: dict, market_id: str, outcome: int, exit_date: str = None):
    """
    Ferme une position et calcule le P&L.

    outcome : 0 = NO gagne (on gagne), 1 = YES gagne (on perd)
    """
    pos = portfolio["positions_ouvertes"].pop(market_id, None)
    if pos is None:
        return

    p_yes  = pos["entry_price_yes"]
    p_no   = 1.0 - p_yes
    bet    = pos["bet_amount"]
    odds_b = p_yes / p_no if p_no > 0 else 0

    if outcome == 0:  # NO gagne → on gagne
        profit_gross = bet * odds_b
        profit_net   = round(profit_gross * (1 - POLYMARKET_FEE), 2)
        win = True
    else:              # YES gagne → on perd
        profit_net = -bet
        win = False

    # Remettre la mise + le profit dans le capital disponible
    portfolio["capital_disponible"] += bet + profit_net

    trade = {
        **pos,
        "exit_date":  exit_date or datetime.now(tz=timezone.utc).isoformat(),
        "outcome":    outcome,
        "profit_net": profit_net,
        "win":        win,
    }
    portfolio["trades_clos"].append(trade)

    emoji = "WIN" if win else "LOSS"
    logger.info(
        f"  [{emoji}] {pos['strategy']:3s} | {pos['question'][:50]:50s} | "
        f"P&L={profit_net:+.2f}$"
    )


def print_summary(portfolio: dict):
    """Affiche un résumé lisible du portefeuille."""
    clos    = portfolio["trades_clos"]
    ouvert  = portfolio["positions_ouvertes"]
    capital_init = portfolio["capital_initial"]
    capital_dispo = portfolio["capital_disponible"]

    # Capital engagé dans les positions ouvertes
    capital_engage = sum(p["bet_amount"] for p in ouvert.values())
    capital_total  = capital_dispo + capital_engage

    # P&L clos
    pnl_clos = sum(t["profit_net"] for t in clos) if clos else 0
    nb_wins  = sum(1 for t in clos if t["win"]) if clos else 0
    nb_total = len(clos)
    wr       = nb_wins / nb_total * 100 if nb_total > 0 else 0

    logger.info("\n" + "=" * 60)
    logger.info("PORTEFEUILLE PAPER TRADING")
    logger.info("=" * 60)
    logger.info(f"  Capital initial     : {capital_init:>10.2f}$")
    logger.info(f"  Capital total       : {capital_total:>10.2f}$")
    logger.info(f"  Capital disponible  : {capital_dispo:>10.2f}$")
    logger.info(f"  Capital engage      : {capital_engage:>10.2f}$ ({len(ouvert)} positions)")
    logger.info(f"  P&L realise         : {pnl_clos:>+10.2f}$")
    logger.info(f"  Rendement           : {(capital_total-capital_init)/capital_init*100:>+9.2f}%")
    logger.info(f"  Trades clos         : {nb_total:>10,}")
    logger.info(f"  Win rate            : {wr:>9.1f}%")

    if ouvert:
        logger.info(f"\n  Positions ouvertes ({len(ouvert)}) :")
        for mid, pos in list(ouvert.items())[:10]:
            logger.info(
                f"    {pos['strategy']:3s} | {pos['question'][:45]:45s} | "
                f"YES={pos['entry_price_yes']:.3f} | Mise={pos['bet_amount']:.2f}$"
            )
        if len(ouvert) > 10:
            logger.info(f"    ... et {len(ouvert)-10} autres")

    # Répartition par stratégie
    if clos:
        logger.info("\n  Performance par strategie :")
        from collections import defaultdict
        stats = defaultdict(lambda: {"wins": 0, "total": 0, "pnl": 0.0})
        for t in clos:
            s = t["strategy"]
            stats[s]["total"] += 1
            stats[s]["wins"]  += int(t["win"])
            stats[s]["pnl"]   += t["profit_net"]
        for s, st in sorted(stats.items()):
            wr_s = st["wins"] / st["total"] * 100 if st["total"] > 0 else 0
            logger.info(
                f"    {s:3s} | {st['total']:>5,} trades | WR {wr_s:>5.1f}% | P&L {st['pnl']:>+8.2f}$"
            )
