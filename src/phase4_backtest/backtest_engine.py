"""
Phase 4 — Moteur de backtest
=============================
Simule chaque stratégie trade par trade avec :
  - Prix d'entrée réaliste (premier trade sous le seuil, ou moyenne des 48h)
  - Kelly Criterion pour le sizing des positions
  - Calcul du P&L cumulé, Sharpe ratio, max drawdown
  - Pas de look-ahead bias : on n'utilise que les infos disponibles à l'entrée

Modèle de trade :
  - On achète des tokens NO à prix p_no = (1 - prix_YES)
  - Si NO gagne : on reçoit 1$ par token → profit = (1 - p_no) / p_no = p_yes / p_no
  - Si YES gagne : on perd la mise (p_no par token)
  - Frais Polymarket : 2% du profit (approximation)

Kelly Criterion :
  f* = (b*p - q) / b
  où b = gain/perte ratio, p = win rate estimé, q = 1-p
  On utilise un Kelly fractionné (25%) pour réduire la variance.
"""

import sys
from pathlib import Path

import pandas as pd
import numpy as np
from loguru import logger

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Frais Polymarket : 2% du profit réalisé
POLYMARKET_FEE = 0.02

# Kelly fraction : on utilise 25% du Kelly full pour limiter le risque
KELLY_FRACTION = 0.25

# Mise minimale et maximale par trade (en % du capital)
MIN_BET_PCT = 0.001   # 0.1% du capital minimum
MAX_BET_PCT = 0.10    # 10% du capital maximum par trade


def kelly_fraction(win_rate: float, odds_b: float) -> float:
    """
    Calcule la fraction Kelly optimale.

    win_rate : probabilité de gagner (entre 0 et 1)
    odds_b   : ratio gain/perte (ex: si on mise 0.95 pour gagner 0.05, b=0.05/0.95)

    Retourne la fraction du capital à miser (entre 0 et MAX_BET_PCT).
    """
    if odds_b <= 0 or win_rate <= 0:
        return 0.0

    q = 1 - win_rate
    # Formule Kelly : f* = (b*p - q) / b
    f_star = (odds_b * win_rate - q) / odds_b

    # Appliquer le Kelly fractionné
    f_kelly = f_star * KELLY_FRACTION

    # Clipper entre les bornes
    return float(np.clip(f_kelly, MIN_BET_PCT, MAX_BET_PCT))


def simulate_trades(trades_df: pd.DataFrame, initial_capital: float = 1000.0) -> dict:
    """
    Simule une séquence de trades et calcule les métriques de performance.

    trades_df doit avoir les colonnes :
      - entry_price_yes : prix YES au moment de l'entrée (float entre 0 et 1)
      - outcome         : 0 = NO gagne, 1 = YES gagne
      - timestamp       : date du trade (pour les métriques temporelles)
      - win_rate_prior  : win rate estimé à priori pour cette stratégie
      - market_id       : identifiant du marché (optionnel)
      - question        : question du marché (optionnel)

    Retourne un dict avec toutes les métriques.
    """
    if trades_df.empty:
        return {"error": "Aucun trade à simuler"}

    capital = initial_capital
    equity_curve = [capital]
    trades_results = []

    for _, row in trades_df.iterrows():
        p_yes = float(row["entry_price_yes"])
        p_no  = 1 - p_yes

        if p_no <= 0 or p_no >= 1:
            continue

        # Odds pour un pari NO : si NO gagne on gagne p_yes, si YES gagne on perd p_no
        # b = gain_si_win / perte_si_loss = p_yes / p_no
        odds_b = p_yes / p_no

        win_rate_prior = float(row.get("win_rate_prior", 0.65))

        # Taille de position via Kelly fractionné
        # On plafonne à initial_capital * MAX_BET_PCT pour éviter la croissance
        # exponentielle irréaliste (liquidité limitée sur Polymarket)
        bet_fraction = kelly_fraction(win_rate_prior, odds_b)
        bet_amount   = min(capital * bet_fraction, initial_capital * MAX_BET_PCT)

        # Résolution du trade
        outcome = int(row["outcome"])  # 0 = NO gagne, 1 = YES gagne

        if outcome == 0:  # NO gagne → on gagne
            profit_gross = bet_amount * odds_b
            profit_net   = profit_gross * (1 - POLYMARKET_FEE)
            capital     += profit_net
            win          = True
        else:             # YES gagne → on perd la mise
            capital -= bet_amount
            profit_net = -bet_amount
            win        = False

        # Garde-fou : capital négatif ou NaN → arrêter la simulation
        if capital <= 0 or not np.isfinite(capital):
            equity_curve.append(max(capital, 0))
            break

        equity_curve.append(capital)
        trades_results.append({
            "market_id":        row.get("market_id", ""),
            "question":         str(row.get("question", ""))[:60],
            "entry_price_yes":  round(p_yes, 4),
            "bet_fraction_pct": round(bet_fraction * 100, 2),
            "bet_amount":       round(bet_amount, 2),
            "outcome":          "NO" if outcome == 0 else "YES",
            "win":              win,
            "profit_net":       round(profit_net, 2),
            "capital_after":    round(capital, 2),
        })

    return compute_metrics(trades_results, equity_curve, initial_capital)


def compute_metrics(trades_results: list, equity_curve: list, initial_capital: float) -> dict:
    """Calcule toutes les métriques de performance depuis les résultats bruts."""
    if not trades_results:
        return {"error": "Aucun résultat"}

    df = pd.DataFrame(trades_results)

    final_capital    = equity_curve[-1]
    total_return_pct = (final_capital - initial_capital) / initial_capital * 100

    wins  = df["win"].sum()
    total = len(df)
    win_rate = wins / total if total > 0 else 0

    # Max drawdown
    eq = np.array(equity_curve)
    peak = np.maximum.accumulate(eq)
    drawdown = (eq - peak) / peak
    max_drawdown_pct = float(drawdown.min() * 100)

    # Profit factor = total gains / total losses
    gains  = df[df["profit_net"] > 0]["profit_net"].sum()
    losses = abs(df[df["profit_net"] < 0]["profit_net"].sum())
    profit_factor = gains / losses if losses > 0 else float("inf")

    # Return moyen par trade
    avg_return_pct = df["profit_net"].sum() / initial_capital / total * 100

    # Sharpe approximatif (sur les profits par trade normalisés)
    returns = df["profit_net"] / initial_capital
    sharpe  = float(returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0

    return {
        "nb_trades":         total,
        "win_rate_pct":      round(win_rate * 100, 2),
        "initial_capital":   round(initial_capital, 2),
        "final_capital":     round(final_capital, 2),
        "total_return_pct":  round(total_return_pct, 2),
        "avg_return_per_trade_pct": round(avg_return_pct, 4),
        "max_drawdown_pct":  round(max_drawdown_pct, 2),
        "profit_factor":     round(profit_factor, 2),
        "sharpe_approx":     round(sharpe, 2),
        "equity_curve":      equity_curve,
        "trades_df":         df,
    }


def print_metrics(name: str, metrics: dict):
    """Affiche les métriques d'une stratégie de façon lisible."""
    if "error" in metrics:
        logger.error(f"{name} : {metrics['error']}")
        return

    logger.info(f"\n{'─'*50}")
    logger.info(f"STRATEGIE : {name}")
    logger.info(f"{'─'*50}")
    logger.info(f"  Nb trades          : {metrics['nb_trades']:>10,}")
    logger.info(f"  Win rate           : {metrics['win_rate_pct']:>9.2f}%")
    logger.info(f"  Capital initial    : {metrics['initial_capital']:>10.0f}$")
    logger.info(f"  Capital final      : {metrics['final_capital']:>10.2f}$")
    logger.info(f"  Rendement total    : {metrics['total_return_pct']:>9.2f}%")
    logger.info(f"  Rendement/trade    : {metrics['avg_return_per_trade_pct']:>9.4f}%")
    logger.info(f"  Max drawdown       : {metrics['max_drawdown_pct']:>9.2f}%")
    logger.info(f"  Profit factor      : {metrics['profit_factor']:>10.2f}")
    logger.info(f"  Sharpe (approx)    : {metrics['sharpe_approx']:>10.2f}")
