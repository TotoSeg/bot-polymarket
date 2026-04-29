"""
Phase 3 — Stratégie volatilité BTC/ETH 5mn (wallet 0x9F5fFE76a818DC)
======================================================================
Analyse le comportement de ce wallet sur les marchés "Up or Down" 5mn
et backteste la stratégie sur l'ensemble des données historiques.

Formule P&L (perspective YES unifiée) :
  BUY  → cash_out = usd_amount ; tokens_in  =  usd/price
  SELL → cash_in  = usd_amount ; tokens_out = usd/price
  Résolution YES (price=1): pnl = +tokens_in - tokens_out + net_cash
  Résolution NO  (price=0): pnl = net_cash = SELL_total - BUY_total
"""

import sys, duckdb, pandas as pd, numpy as np, matplotlib
matplotlib.use("Agg"); import matplotlib.pyplot as plt
from pathlib import Path; from datetime import datetime
from loguru import logger

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WALLET  = "0x9F5fFE76a818DCE37c70F947998b52b70671A008"
ROOT    = Path(__file__).resolve().parents[2]
OUT     = ROOT / "outputs" / "phase3" / "btc_vol"
OUT.mkdir(parents=True, exist_ok=True)
LOGS    = ROOT / "logs"
LOGS.mkdir(exist_ok=True)

logger.remove()
logger.add(sys.stdout, colorize=True, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
logger.add(LOGS / f"{datetime.now().strftime('%Y-%m-%d')}_btc_vol.log",
           format="{time} | {level} | {message}")


def load_wallet_trades(con) -> pd.DataFrame:
    """Tous les trades du wallet sur marchés BTC/ETH Up or Down résolus."""
    df = con.execute(f"""
        SELECT q.side, q.price, q.usd_amount,
               q.usd_amount / q.price AS token_amount,
               m.question, m.outcome_prices,
               epoch_ms(CAST(q.timestamp AS BIGINT)*1000) AS dt,
               m.id AS market_id
        FROM read_parquet('data/quant.parquet') q
        JOIN read_parquet('data/markets.parquet') m ON q.market_id = m.id
        WHERE (q.maker='{WALLET}' OR q.taker='{WALLET}')
          AND (m.question ILIKE '%Up or Down%')
          AND q.price > 0 AND q.price < 1
          AND q.usd_amount > 0
          AND m.closed = 1
        ORDER BY q.timestamp
    """).df()
    logger.info(f"Trades chargés : {len(df):,} sur {df['market_id'].nunique()} marchés")
    return df


def parse_outcome(outcome_str: str):
    """Retourne (yes_price, no_price) depuis outcome_prices string."""
    try:
        import json
        vals = json.loads(outcome_str.replace("'", '"'))
        return float(vals[0]), float(vals[1])
    except Exception:
        return None, None


def compute_market_pnl(group: pd.DataFrame, yes_price_res: float) -> dict:
    """
    P&L d'un marché donné pour ce wallet.

    yes_price_res = 1.0 si YES gagne, 0.0 si NO gagne.
    """
    buy  = group[group["side"] == "BUY"]
    sell = group[group["side"] == "SELL"]

    buy_usd   = buy["usd_amount"].sum()
    sell_usd  = sell["usd_amount"].sum()
    buy_tok   = buy["token_amount"].sum()
    sell_tok  = sell["token_amount"].sum()

    # Flux de trésorerie net
    net_cash   = sell_usd - buy_usd
    # Tokens YES nets détenus à la résolution
    net_tokens = buy_tok - sell_tok

    # P&L total = cash net + valeur résiduelle des tokens
    pnl = net_cash + net_tokens * yes_price_res

    return {
        "buy_usd":   buy_usd,
        "sell_usd":  sell_usd,
        "net_cash":  net_cash,
        "net_tokens": net_tokens,
        "yes_res":   yes_price_res,
        "pnl":       pnl,
        "n_trades":  len(group),
    }


def backtest(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcule le P&L par marché et retourne un DataFrame de résultats.
    Exclut les marchés non résolus clairement (outcome_prices ≈ 0.5).
    """
    results = []
    for mid, grp in df.groupby("market_id"):
        op = grp["outcome_prices"].iloc[0]
        yp, np_ = parse_outcome(op)
        if yp is None:
            continue
        # Exclure les marchés sans résolution binaire claire
        if abs(yp - 0.5) < 0.4 and abs(np_ - 0.5) < 0.4:
            continue  # outcome_prices ~ [0.5, 0.5] = push / non résolu
        yes_res = 1.0 if yp >= 0.99 else 0.0

        r = compute_market_pnl(grp, yes_res)
        r["market_id"] = mid
        r["question"]  = grp["question"].iloc[0][:60]
        r["outcome"]   = "YES" if yes_res == 1.0 else "NO"
        r["dt"]        = grp["dt"].iloc[0]
        results.append(r)

    return pd.DataFrame(results).sort_values("dt")


def describe_strategy(df_trades: pd.DataFrame, df_results: pd.DataFrame):
    """Affiche les statistiques clés de la stratégie."""
    logger.info("=" * 60)
    logger.info("STRATÉGIE WALLET 0x9F5fFE76a818DC")
    logger.info("=" * 60)

    # Distribution BUY vs SELL
    sides = df_trades["side"].value_counts()
    logger.info(f"\nRépartition trades :")
    logger.info(f"  BUY  : {sides.get('BUY',0):,} trades | "
                f"{df_trades[df_trades.side=='BUY']['usd_amount'].sum():,.0f}$ déployés")
    logger.info(f"  SELL : {sides.get('SELL',0):,} trades | "
                f"{df_trades[df_trades.side=='SELL']['usd_amount'].sum():,.0f}$ déployés")

    # Distribution des prix tradés
    logger.info(f"\nDistribution des prix YES tradés :")
    for bracket, lo, hi in [("<10%",0,.10), ("10-30%",.10,.30),
                             ("30-70%",.30,.70), ("70-90%",.70,.90), (">90%",.90,1.01)]:
        n = ((df_trades.price >= lo) & (df_trades.price < hi)).sum()
        v = df_trades[(df_trades.price >= lo) & (df_trades.price < hi)]["usd_amount"].sum()
        logger.info(f"  {bracket:8s}: {n:,} trades | {v:,.0f}$")

    # Timing des trades dans la fenêtre
    df_trades["minute_in_window"] = df_trades["dt"].dt.minute
    logger.info(f"\nConcentration des trades (minute de la fenêtre) :")
    mc = df_trades.groupby("minute_in_window")["usd_amount"].sum().sort_index()
    for m, v in mc.items():
        logger.info(f"  Minute {m:02d} : {v:,.0f}$")


def report_backtest(df_results: pd.DataFrame):
    """Rapport complet du backtest."""
    if df_results.empty:
        logger.warning("Aucun résultat")
        return

    logger.info("=" * 60)
    logger.info("BACKTEST — RÉSULTATS")
    logger.info("=" * 60)

    total_pnl    = df_results["pnl"].sum()
    n_markets    = len(df_results)
    n_profit     = (df_results["pnl"] > 0).sum()
    win_rate     = n_profit / n_markets
    vol_deployed = df_results["buy_usd"].sum() + df_results["sell_usd"].sum()

    logger.info(f"  Marchés analysés  : {n_markets:,}")
    logger.info(f"  P&L total         : {total_pnl:+,.2f}$")
    logger.info(f"  Win rate marché   : {win_rate:.1%}  ({n_profit}/{n_markets})")
    logger.info(f"  Volume déployé    : {vol_deployed:,.0f}$")
    logger.info(f"  ROI               : {total_pnl/vol_deployed*100:+.2f}%"
                if vol_deployed > 0 else "")

    # Décomposition YES/NO outcomes
    for outcome in ["YES", "NO"]:
        sub = df_results[df_results["outcome"] == outcome]
        if sub.empty:
            continue
        logger.info(f"\n  Marchés résolus {outcome} ({len(sub)}) :")
        logger.info(f"    P&L moyen     : {sub['pnl'].mean():+.2f}$")
        logger.info(f"    Win rate      : {(sub['pnl']>0).mean():.1%}")
        logger.info(f"    net_cash moyen: {sub['net_cash'].mean():+.2f}$")

    # Top 5 meilleurs et pires marchés
    logger.info("\n  Top 5 marchés (P&L) :")
    for _, r in df_results.nlargest(5, "pnl").iterrows():
        logger.info(f"    {r['pnl']:+7.2f}$ | {r['outcome']} | {r['question'][:50]}")
    logger.info("\n  Pires 5 marchés :")
    for _, r in df_results.nsmallest(5, "pnl").iterrows():
        logger.info(f"    {r['pnl']:+7.2f}$ | {r['outcome']} | {r['question'][:50]}")


def plot_results(df_results: pd.DataFrame):
    """Courbe equity + distribution P&L."""
    df = df_results.sort_values("dt").copy()
    df["cum_pnl"] = df["pnl"].cumsum()

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    axes[0].plot(df["dt"], df["cum_pnl"], linewidth=1.5, color="steelblue")
    axes[0].axhline(0, color="red", linestyle="--", alpha=0.5)
    axes[0].fill_between(df["dt"], df["cum_pnl"], 0,
                         where=df["cum_pnl"]>0, color="green", alpha=0.2)
    axes[0].fill_between(df["dt"], df["cum_pnl"], 0,
                         where=df["cum_pnl"]<0, color="red",   alpha=0.2)
    axes[0].set_title(f"P&L cumulé — {WALLET[:20]}...")
    axes[0].set_ylabel("P&L ($)")

    axes[1].hist(df["pnl"], bins=30, color="steelblue", edgecolor="white", alpha=0.8)
    axes[1].axvline(0, color="red", linestyle="--")
    axes[1].set_title("Distribution P&L par marché")
    axes[1].set_xlabel("P&L ($)")

    plt.tight_layout()
    p = OUT / "btc_vol_equity.png"
    plt.savefig(p, dpi=150); plt.close()
    logger.info(f"Graphique → {p}")


def main():
    con = duckdb.connect()
    logger.info(f"Analyse du wallet {WALLET[:20]}...")

    df_trades  = load_wallet_trades(con)

    if df_trades.empty:
        logger.error("Aucun trade trouvé")
        return

    df_results = backtest(df_trades)
    describe_strategy(df_trades, df_results)
    report_backtest(df_results)
    plot_results(df_results)

    df_results.to_csv(OUT / "wallet_pnl_per_market.csv", index=False)
    logger.success("Analyse terminée.")


if __name__ == "__main__":
    main()
