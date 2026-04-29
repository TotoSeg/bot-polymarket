"""
Phase 3 — Stratégie "Whale Copy-Trading" (S_WHALE)
====================================================
Hypothèse : une minorité de wallets obtiennent des win rates >> 50% de façon
persistante. En copiant leurs paris, on peut surperformer le marché.

Méthodologie (walk-forward pour éviter le look-ahead bias) :
  1. Identification   : période 2021-2023 → classer les wallets par WR + profit factor
  2. Sélection        : garder les wallets avec WR > 60%, profit_factor > 1.5, ≥ 20 trades
  3. Test             : période 2024-2025 → simuler la copie de leurs trades
                        Taille fixe : 100$ par trade copié

Données utilisées :
  - data/quant.parquet : 170M trades (maker, taker, side, price, usd_amount, timestamp)
  - data/markets.parquet : 684K marchés fermés avec outcome_prices

Résultats attendus (selon recherche) :
  - Top wallets : WR 60-75%, P&L cumulé significatif
  - Copy-trading : reproduire ~70% du WR des top wallets (dilution due au lag)
"""

import sys
import time
import json
from pathlib import Path
from datetime import datetime, timezone

import duckdb
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from loguru import logger

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR     = PROJECT_ROOT / "data"
OUT_DIR      = PROJECT_ROOT / "outputs" / "phase3" / "whale"
LOGS_DIR     = PROJECT_ROOT / "logs"
QUANT        = str(DATA_DIR / "quant.parquet")
MARKETS      = str(DATA_DIR / "markets.parquet")

QUANT_PATH   = QUANT.replace("\\", "/")
MARKETS_PATH = MARKETS.replace("\\", "/")

# ── Paramètres de la stratégie ───────────────────────────────────────────────
MIN_TRADES_WALLET   = 20     # Minimum de trades pour qualifier un wallet
MIN_WIN_RATE        = 0.60   # Win rate minimum en période d'identification
MIN_PROFIT_FACTOR   = 1.50   # Ratio gains/pertes minimum
MIN_USD_PER_TRADE   = 10.0   # Ignorer les micro-trades (bruit)
COPY_BET_SIZE       = 100.0  # Mise fixe pour chaque trade copié (en $)
MAX_COPY_WALLETS    = 50     # Copier au maximum N wallets simultanément

# Split temporel : train=avant, test=après
# Le timestamp dans quant.parquet est en secondes (Unix) — à vérifier
SPLIT_DATE_TRAIN_END = datetime(2024, 1, 1, tzinfo=timezone.utc)
SPLIT_DATE_TEST_END  = datetime(2025, 12, 31, tzinfo=timezone.utc)


def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    logger.remove()
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_whale_copying.log"
    logger.add(sys.stdout, colorize=True, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add(log_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")


def detect_timestamp_unit(con: duckdb.DuckDBPyConnection) -> str:
    """Détermine si le timestamp est en secondes ou millisecondes."""
    ts = con.execute(f"SELECT timestamp FROM read_parquet('{QUANT_PATH}') LIMIT 1").fetchone()[0]
    ts_dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    if 2000 <= ts_dt.year <= 2030:
        return "ms"   # millisecondes → diviser par 1000
    ts_dt2 = datetime.fromtimestamp(ts, tz=timezone.utc)
    if 2000 <= ts_dt2.year <= 2030:
        return "s"    # secondes
    return "ms"


# ── Étape 1 : Charger les outcomes des marchés fermés ─────────────────────────

def load_market_outcomes(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """
    Retourne un DataFrame (market_id, yes_wins) pour tous les marchés résolus.
    yes_wins : 1 = YES a gagné, 0 = NO a gagné.

    outcome_prices format : "['1', '0']" (YES wins) ou "['0', '1']" (NO wins).
    Le premier élément = prix final du token YES après résolution.
    On utilise LIKE avec '' (guillemets SQL doublés) pour matcher les guillemets simples.
    """
    logger.info("Chargement des outcomes marchés...")
    # Utiliser LIKE avec '' pour échapper les guillemets simples dans DuckDB
    df = con.execute(f"""
        SELECT id AS market_id,
            CASE WHEN outcome_prices LIKE '[''1''%' THEN 1
                 WHEN outcome_prices LIKE '[''0''%' THEN 0 END AS yes_wins
        FROM read_parquet('{MARKETS_PATH}')
        WHERE closed = 1
          AND (outcome_prices LIKE '[''1''%' OR outcome_prices LIKE '[''0''%')
    """).df()

    df = df.dropna(subset=["yes_wins"])
    df["yes_wins"] = df["yes_wins"].astype(int)
    logger.info(f"Marchés résolus : {len(df):,} "
                f"(YES wins: {df['yes_wins'].sum():,}, NO wins: {(df['yes_wins']==0).sum():,})")
    return df[["market_id", "yes_wins"]]


# ── Étape 2 : Calcul des métriques par wallet ─────────────────────────────────

def compute_wallet_stats(con: duckdb.DuckDBPyConnection,
                         outcomes: pd.DataFrame,
                         ts_unit: str,
                         period_end_ts: int) -> pd.DataFrame:
    """
    Calcule les métriques de performance par wallet maker sur une période donnée.

    Pour chaque trade (maker, market_id, side, price) :
      - side='YES' + yes_wins=1 → le maker a parié YES et gagné
      - side='NO'  + yes_wins=0 → le maker a parié NO et gagné
      - P&L estimé (taille normalisée à 1$) :
          WIN YES  : (1/price - 1)
          WIN NO   : (price / (1-price))
          LOSS     : -1

    Retourne un DataFrame par wallet avec nb_trades, win_rate, profit_factor, pnl_1$
    """
    logger.info(f"Calcul des métriques wallets (avant ts={period_end_ts})...")

    # Écrire les outcomes en table temporaire DuckDB pour le JOIN
    con.register("outcomes_df", outcomes)

    ts_filter = f"q.timestamp <= {period_end_ts}"

    df = con.execute(f"""
        WITH joined AS (
            SELECT
                q.maker,
                q.side,
                q.price,
                q.usd_amount,
                o.yes_wins
            FROM read_parquet('{QUANT_PATH}') q
            JOIN outcomes_df o ON q.market_id = o.market_id
            WHERE {ts_filter}
              AND q.price > 0.01 AND q.price < 0.99
              AND q.usd_amount >= {MIN_USD_PER_TRADE}
              AND q.maker IS NOT NULL
        ),
        with_result AS (
            SELECT
                maker,
                -- Dans quant.parquet : side='BUY' = achat YES, side='SELL' = vente YES (= pari NO)
                -- Gagné si BUY & YES wins, ou SELL & NO wins (YES perd)
                CASE
                    WHEN side = 'BUY'  AND yes_wins = 1 THEN 1
                    WHEN side = 'SELL' AND yes_wins = 0 THEN 1
                    ELSE 0
                END AS won,
                -- P&L normalisé à 1$ misé
                -- BUY YES à prix p : gain = (1/p - 1) si win, perte = -1 si loss
                -- SELL YES à prix p (= achat NO à 1-p) : gain = p/(1-p) si win, perte = -1 si loss
                CASE
                    WHEN side = 'BUY'  AND yes_wins = 1 THEN (1.0/price - 1)
                    WHEN side = 'SELL' AND yes_wins = 0 THEN price/(1.0 - price)
                    ELSE -1.0
                END AS pnl_norm,
                usd_amount
            FROM joined
        )
        SELECT
            maker,
            COUNT(*)                                          AS nb_trades,
            AVG(won)                                         AS win_rate,
            SUM(CASE WHEN pnl_norm > 0 THEN pnl_norm ELSE 0 END)
                / NULLIF(SUM(CASE WHEN pnl_norm < 0 THEN ABS(pnl_norm) ELSE 0 END), 0)
                                                             AS profit_factor,
            SUM(pnl_norm)                                    AS pnl_1dollar,
            SUM(usd_amount)                                  AS total_volume_usd
        FROM with_result
        GROUP BY maker
        HAVING COUNT(*) >= {MIN_TRADES_WALLET}
    """).df()

    logger.info(f"Wallets qualifiés (>= {MIN_TRADES_WALLET} trades) : {len(df):,}")
    return df


# ── Étape 3 : Sélection des top wallets ───────────────────────────────────────

def select_top_wallets(wallet_stats: pd.DataFrame) -> pd.DataFrame:
    """
    Filtre les wallets qualifiés pour le copy-trading.

    Critères :
      - win_rate >= MIN_WIN_RATE
      - profit_factor >= MIN_PROFIT_FACTOR
      - Tri par profit_factor × win_rate (score composite)
    """
    top = wallet_stats[
        (wallet_stats["win_rate"] >= MIN_WIN_RATE) &
        (wallet_stats["profit_factor"] >= MIN_PROFIT_FACTOR)
    ].copy()

    top["score"] = top["win_rate"] * top["profit_factor"]
    top = top.sort_values("score", ascending=False).head(MAX_COPY_WALLETS)

    logger.info(f"Top wallets sélectionnés pour copy-trading : {len(top)}")
    if len(top) > 0:
        logger.info(f"  WR médian    : {top['win_rate'].median():.1%}")
        logger.info(f"  PF médian    : {top['profit_factor'].median():.2f}")
        logger.info(f"  Trades médian: {top['nb_trades'].median():.0f}")
        logger.info(f"  Top 5 scores :")
        for _, row in top.head(5).iterrows():
            logger.info(f"    {row['maker'][:20]}... | WR={row['win_rate']:.1%} | "
                        f"PF={row['profit_factor']:.2f} | Trades={row['nb_trades']:.0f}")
    return top


# ── Étape 4 : Backtest copy-trading (période test) ────────────────────────────

def backtest_copy_trading(con: duckdb.DuckDBPyConnection,
                          top_wallets: pd.DataFrame,
                          outcomes: pd.DataFrame,
                          ts_unit: str,
                          ts_train_end: int,
                          ts_test_end: int) -> dict:
    """
    Simule le copy-trading sur la période test.

    Règle : dès qu'un top wallet passe un ordre, on copie avec COPY_BET_SIZE $.
    Taille fixe (pas de Kelly pour simplifier — on veut mesurer le signal pur).
    """
    if len(top_wallets) == 0:
        logger.warning("Aucun top wallet → pas de backtest possible")
        return {}

    logger.info(f"Backtest copy-trading sur période test [{ts_train_end} → {ts_test_end}]...")

    top_list = top_wallets["maker"].tolist()
    con.register("outcomes_df", outcomes)

    # Récupérer les trades des top wallets sur la période test
    placeholders = ", ".join([f"'{w}'" for w in top_list])

    df = con.execute(f"""
        SELECT
            q.maker,
            q.market_id,
            q.side,
            q.price,
            q.usd_amount,
            q.timestamp,
            o.yes_wins
        FROM read_parquet('{QUANT_PATH}') q
        JOIN outcomes_df o ON q.market_id = o.market_id
        WHERE q.maker IN ({placeholders})
          AND q.timestamp > {ts_train_end}
          AND q.timestamp <= {ts_test_end}
          AND q.price > 0.01 AND q.price < 0.99
          AND q.usd_amount >= {MIN_USD_PER_TRADE}
    """).df()

    if len(df) == 0:
        logger.warning("Aucun trade des top wallets sur la période test")
        return {}

    logger.info(f"Trades des top wallets (période test) : {len(df):,}")

    # Calculer le résultat de chaque trade copié
    df["won"] = (
        ((df["side"] == "BUY")  & (df["yes_wins"] == 1)) |
        ((df["side"] == "SELL") & (df["yes_wins"] == 0))
    ).astype(int)

    # P&L par trade copié à COPY_BET_SIZE $
    df["pnl"] = df.apply(lambda r: (
        COPY_BET_SIZE * (1.0/r["price"] - 1)        if r["side"] == "BUY"  and r["won"] == 1 else
        COPY_BET_SIZE * (r["price"]/(1-r["price"]))  if r["side"] == "SELL" and r["won"] == 1 else
        -COPY_BET_SIZE
    ), axis=1)

    # Résumé
    nb_trades  = len(df)
    nb_wins    = df["won"].sum()
    win_rate   = nb_wins / nb_trades
    total_pnl  = df["pnl"].sum()
    profit_fac = (df.loc[df["pnl"] > 0, "pnl"].sum() /
                  abs(df.loc[df["pnl"] < 0, "pnl"].sum())
                  if (df["pnl"] < 0).any() else float("inf"))

    capital_deployed = nb_trades * COPY_BET_SIZE
    roi = total_pnl / capital_deployed * 100

    logger.info("=" * 60)
    logger.info("RÉSULTATS COPY-TRADING (période test)")
    logger.info("=" * 60)
    logger.info(f"  Trades copiés    : {nb_trades:,}")
    logger.info(f"  Wallets copiés   : {df['maker'].nunique()}")
    logger.info(f"  Win rate         : {win_rate:.1%}")
    logger.info(f"  Profit factor    : {profit_fac:.2f}")
    logger.info(f"  P&L total        : {total_pnl:+,.2f}$")
    logger.info(f"  Capital déployé  : {capital_deployed:,.0f}$")
    logger.info(f"  ROI              : {roi:+.2f}%")

    # Distribution par wallet
    df["rank_wallet"] = df["maker"].map(
        dict(zip(top_wallets["maker"], range(len(top_wallets))))
    )
    by_wallet = df.groupby("maker").agg(
        nb_trades=("won", "count"),
        win_rate=("won", "mean"),
        pnl=("pnl", "sum"),
    ).sort_values("pnl", ascending=False)

    logger.info("\n  Top 10 wallets copiés :")
    for addr, row in by_wallet.head(10).iterrows():
        logger.info(f"    {addr[:20]}... | WR={row['win_rate']:.1%} | "
                    f"Trades={int(row['nb_trades']):3d} | P&L={row['pnl']:+.2f}$")

    return {
        "nb_trades":        nb_trades,
        "nb_wallets":       df["maker"].nunique(),
        "win_rate":         win_rate,
        "profit_factor":    profit_fac,
        "total_pnl":        total_pnl,
        "capital_deployed": capital_deployed,
        "roi":              roi,
        "trades_df":        df,
        "by_wallet":        by_wallet,
    }


# ── Étape 5 : Analyse complémentaire — distribution des WR ──────────────────

def analyze_wallet_distribution(wallet_stats: pd.DataFrame):
    """
    Analyse la distribution des win rates pour valider l'hypothèse de persistance.
    Si les meilleurs wallets existent par chance, on attend une distribution normale.
    Si un edge réel existe, on voit une queue droite lourde (distribution asymétrique).
    """
    logger.info("\n=== Distribution des Win Rates par wallet ===")
    wr = wallet_stats["win_rate"].dropna()

    percentiles = [50, 75, 90, 95, 99]
    for p in percentiles:
        v = np.percentile(wr, p)
        logger.info(f"  Percentile {p:2d}% : {v:.1%}")

    n_above_60 = (wr >= 0.60).sum()
    n_above_70 = (wr >= 0.70).sum()
    logger.info(f"\n  Wallets WR >= 60% : {n_above_60:,} ({n_above_60/len(wr):.2%})")
    logger.info(f"  Wallets WR >= 70% : {n_above_70:,} ({n_above_70/len(wr):.2%})")

    # Histogramme
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    axes[0].hist(wr, bins=50, color="steelblue", edgecolor="white", alpha=0.8)
    axes[0].axvline(0.5, color="red",    linestyle="--", label="50% (random)")
    axes[0].axvline(0.6, color="orange", linestyle="--", label="60% (seuil sélection)")
    axes[0].set_xlabel("Win Rate")
    axes[0].set_ylabel("Nombre de wallets")
    axes[0].set_title(f"Distribution WR — {len(wr):,} wallets (≥{MIN_TRADES_WALLET} trades)")
    axes[0].legend()

    # Volume total vs P&L par wallet (top 200)
    top200 = wallet_stats.nlargest(200, "pnl_1dollar")
    axes[1].scatter(top200["total_volume_usd"], top200["pnl_1dollar"],
                    c=top200["win_rate"], cmap="RdYlGn", alpha=0.7, s=20)
    axes[1].set_xlabel("Volume total ($)")
    axes[1].set_ylabel("P&L normalisé ($1 par trade)")
    axes[1].set_title("Top 200 wallets : Volume vs P&L")
    axes[1].set_xscale("log")

    plt.tight_layout()
    path = OUT_DIR / "whale_distribution.png"
    plt.savefig(path, dpi=150)
    plt.close()
    logger.info(f"Graphique sauvegardé → {path}")


# ── Étape 6 : Courbe equity du copy-trading ───────────────────────────────────

def plot_copy_equity(results: dict, ts_unit: str):
    """Trace la courbe du capital cumulé du portefeuille de copy-trading."""
    if not results or "trades_df" not in results:
        return

    df = results["trades_df"].sort_values("timestamp")

    # Convertir timestamp en datetime
    if ts_unit == "ms":
        df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    else:
        df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)

    df["cumulative_pnl"] = df["pnl"].cumsum()
    df["equity"] = COPY_BET_SIZE + df["cumulative_pnl"]   # capital fictif départ = 1 trade

    # Resampler par semaine pour lisser
    df_weekly = df.set_index("dt")["pnl"].resample("W").sum().cumsum()

    fig, axes = plt.subplots(2, 1, figsize=(14, 8))

    axes[0].plot(df["dt"], df["cumulative_pnl"], color="steelblue", linewidth=1)
    axes[0].axhline(0, color="red", linestyle="--", alpha=0.5)
    axes[0].fill_between(df["dt"], df["cumulative_pnl"], 0,
                         where=df["cumulative_pnl"] > 0, color="green", alpha=0.2)
    axes[0].fill_between(df["dt"], df["cumulative_pnl"], 0,
                         where=df["cumulative_pnl"] < 0, color="red", alpha=0.2)
    axes[0].set_title(f"P&L cumulé — Copy Trading (WR={results['win_rate']:.1%}, "
                      f"ROI={results['roi']:+.1f}%)")
    axes[0].set_ylabel("P&L cumulé ($)")

    # WR glissant (fenêtre 50 trades)
    if len(df) >= 50:
        df["rolling_wr"] = df["won"].rolling(50).mean()
        axes[1].plot(df["dt"], df["rolling_wr"], color="darkorange", linewidth=1)
        axes[1].axhline(0.5, color="gray",  linestyle="--", alpha=0.5, label="50% (random)")
        axes[1].axhline(results["win_rate"], color="green", linestyle="--",
                        alpha=0.7, label=f"WR global {results['win_rate']:.1%}")
        axes[1].set_ylim(0.3, 0.9)
        axes[1].set_ylabel("Win rate glissant (50 trades)")
        axes[1].legend()

    plt.tight_layout()
    path = OUT_DIR / "whale_equity_curve.png"
    plt.savefig(path, dpi=150)
    plt.close()
    logger.info(f"Courbe equity sauvegardée → {path}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    setup()
    logger.info("=" * 60)
    logger.info("WHALE COPY-TRADING ANALYSIS")
    logger.info("=" * 60)

    con = duckdb.connect()

    # 1. Détecter l'unité du timestamp
    logger.info("Détection de l'unité du timestamp...")
    ts_unit = detect_timestamp_unit(con)
    logger.info(f"  Timestamp unit : {ts_unit}")

    # Convertir les dates de split en entiers timestamp
    split_ts = int(SPLIT_DATE_TRAIN_END.timestamp())
    end_ts   = int(SPLIT_DATE_TEST_END.timestamp())
    if ts_unit == "ms":
        split_ts *= 1000
        end_ts   *= 1000

    logger.info(f"  Split train/test : {SPLIT_DATE_TRAIN_END.date()} (ts={split_ts})")
    logger.info(f"  Fin période test : {SPLIT_DATE_TEST_END.date()}  (ts={end_ts})")

    # 2. Charger les outcomes
    outcomes = load_market_outcomes(con)

    # 3. Calculer les métriques wallets sur la période d'identification
    logger.info("\n--- PÉRIODE D'IDENTIFICATION (train) ---")
    wallet_stats = compute_wallet_stats(con, outcomes, ts_unit, split_ts)

    if len(wallet_stats) == 0:
        logger.error("Aucun wallet qualifié — vérifier les données")
        return

    # 4. Analyser la distribution
    analyze_wallet_distribution(wallet_stats)

    # 5. Sélectionner les top wallets
    logger.info("\n--- SÉLECTION TOP WALLETS ---")
    top_wallets = select_top_wallets(wallet_stats)

    # Sauvegarder les top wallets
    top_wallets.to_csv(OUT_DIR / "top_wallets.csv", index=False)
    logger.info(f"Top wallets sauvegardés → {OUT_DIR / 'top_wallets.csv'}")

    # 6. Backtest copy-trading sur la période test
    logger.info("\n--- BACKTEST COPY-TRADING (test) ---")
    results = backtest_copy_trading(con, top_wallets, outcomes, ts_unit, split_ts, end_ts)

    # 7. Courbe equity
    if results:
        plot_copy_equity(results, ts_unit)

    # 8. Comparaison rapide : copy-trading vs stratégie aléatoire
    if results:
        logger.info("\n=== COMPARAISON AVEC BENCHMARK ===")
        # Le benchmark "random" : WR = 50% (random), PF = 1.0
        # Notre stratégie : WR = results["win_rate"], PF = results["profit_factor"]
        logger.info(f"  Copy-trading   : WR={results['win_rate']:.1%}, PF={results['profit_factor']:.2f}")
        logger.info(f"  Benchmark (rand): WR=50.0%, PF=1.00")
        logger.info(f"  Surperformance WR : +{(results['win_rate']-0.5)*100:.1f}pp")

    # 9. Test de robustesse : seuils min_trades plus élevés
    logger.info("\n--- ROBUSTESSE : impact du seuil min_trades ---")
    global MIN_TRADES_WALLET
    for min_t in [50, 100, 200]:
        MIN_TRADES_WALLET = min_t
        ws2 = compute_wallet_stats(con, outcomes, ts_unit, split_ts)
        top2 = select_top_wallets(ws2)
        if len(top2) > 0:
            res2 = backtest_copy_trading(con, top2, outcomes, ts_unit, split_ts, end_ts)
            wr2  = res2.get("win_rate", 0)
            roi2 = res2.get("roi", 0)
        else:
            wr2, roi2 = 0, 0
        logger.info(f"  min_trades={min_t:3d} → top wallets={len(top2):2d} | "
                    f"test WR={wr2:.1%} | ROI={roi2:+.1f}%")
    MIN_TRADES_WALLET = 20  # reset

    # 10. Synthèse finale
    logger.info("\n=== SYNTHÈSE ===")
    if results and results.get("win_rate", 0) > 0.55:
        logger.success(f"EDGE CONFIRMÉ : WR={results['win_rate']:.1%} > 55% sur période test")
        logger.success(f"Stratégie S_WHALE opérationnelle — ROI={results.get('roi', 0):+.2f}%")
    else:
        logger.warning("Edge faible (survivorship bias probable)")
        if results:
            logger.warning(f"WR={results.get('win_rate', 0):.1%}, ROI={results.get('roi', 0):+.2f}%")
            logger.warning("→ Les top wallets en training (20 trades) reviennent vers 50% en test")
            logger.warning("→ Augmenter min_trades ou filtrer par taille de mise (>1K$) pour réduire le bruit")

    logger.success("Analyse whale copy-trading terminée.")


if __name__ == "__main__":
    main()
