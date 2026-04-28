"""
Phase 3 — Edge BTC Up/Down : arbitrage Polymarket vs prix réel
===============================================================
Les marchés "Up or Down" crypto sur Polymarket résolvent selon la direction
du prix BTC sur une période donnée. Ce script étudie si le prix Polymarket
reflète correctement la probabilité réelle, et identifie des strategies
exploitables.

4 STRATÉGIES TESTÉES :
─────────────────────
  SA — Contre-momentum (mean reversion)
       Si BTC a monté de +X% avant la résolution → parier DOWN (NO sur YES=UP)
       Hypothèse : les mouvements courts se corrigent (reversion)

  SB — Momentum (continuation)
       Si BTC a monté → parier UP (YES sur YES=UP)
       Hypothèse : les tendances persistent sur les marchés prédiction

  SC — Mispricing (Polymarket sous-évalue le momentum)
       Si BTC a monté +X% mais Polymarket ne valorise YES (UP) qu'à < 45% →
       parier YES (sous-évalué par rapport au signal réel)

  SD — Arbitrage somme < 1
       Si YES_price + NO_price < 0.98 → acheter les deux côtés →
       profit garanti après frais quelle que soit la résolution

DONNÉES :
  - Polymarket : quant.parquet + markets.parquet
  - Prix BTC   : Binance API klines 5 minutes (avec cache local)

Usage :
    python src/phase3_edges/edge_btc_updown.py
"""

import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import duckdb
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from typing import Optional
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.phase5_paper.btc_price_feed import get_klines_range, compute_indicators

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
OUT_DIR      = PROJECT_ROOT / "outputs" / "phase3" / "btc_updown"

MARKETS = str(DATA_DIR / "markets.parquet")
QUANT   = str(DATA_DIR / "quant.parquet")

POLYMARKET_FEE = 0.02
INITIAL_CAPITAL = 1000.0


def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_edge_btc_updown.log"
    logger.remove()
    logger.add(sys.stdout, colorize=True, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add(log_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
    return log_file


def save_fig(fig, name: str):
    p = OUT_DIR / f"{name}.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.success(f"Graphique : {p.relative_to(PROJECT_ROOT)}")


# ── 1. Chargement des marchés BTC Up/Down ─────────────────────────────────────

def load_btc_updown_markets(con) -> pd.DataFrame:
    """
    Charge tous les marchés 'Up or Down' crypto depuis markets.parquet.
    Filtre sur Bitcoin uniquement (btc, bitcoin).
    Retourne aussi le prix YES moyen sur les dernières heures avant résolution
    (depuis quant.parquet).
    """
    logger.info("=== 1. Chargement des marchés BTC Up/Down ===")

    df = con.execute(f"""
        WITH last_prices AS (
            -- Dernier prix YES avant résolution et prix à mi-vie du marché
            SELECT
                q.market_id,
                LAST(q.price ORDER BY q.timestamp)                         AS last_price_yes,
                FIRST(q.price ORDER BY q.timestamp)                        AS first_price_yes,
                AVG(q.price)                                                AS avg_price_yes,
                -- Prix YES dans les dernières 6h avant résolution
                AVG(CASE
                    WHEN epoch_ms(q.timestamp::BIGINT*1000) >=
                         (m.end_date::TIMESTAMP - INTERVAL '6 hours')
                    THEN q.price END)                                       AS price_last_6h,
                -- Prix YES entre 24h et 6h avant résolution
                AVG(CASE
                    WHEN epoch_ms(q.timestamp::BIGINT*1000) BETWEEN
                         (m.end_date::TIMESTAMP - INTERVAL '24 hours') AND
                         (m.end_date::TIMESTAMP - INTERVAL '6 hours')
                    THEN q.price END)                                       AS price_6_24h,
                MIN(q.price)                                                AS min_price_yes,
                MAX(q.price)                                                AS max_price_yes,
                COUNT(*)                                                    AS nb_trades,
                SUM(q.usd_amount)                                           AS volume_trades
            FROM read_parquet('{QUANT}') q
            JOIN read_parquet('{MARKETS}') m ON m.id = q.market_id
            WHERE (LOWER(m.question) LIKE '%up or down%'
                   OR LOWER(m.question) LIKE '%updown%')
              AND (LOWER(m.question) LIKE '%bitcoin%'
                   OR LOWER(m.question) LIKE '% btc%')
            GROUP BY q.market_id
        )
        SELECT
            m.id                                                            AS market_id,
            m.question,
            CAST(SUBSTRING(m.outcome_prices,3,1) AS INTEGER)               AS outcome,
            m.end_date,
            m.created_at,
            ROUND(m.volume, 0)                                              AS volume,
            lp.last_price_yes,
            lp.first_price_yes,
            lp.avg_price_yes,
            lp.price_last_6h,
            lp.price_6_24h,
            lp.min_price_yes,
            lp.max_price_yes,
            lp.nb_trades,
            ROUND(lp.volume_trades, 0)                                      AS volume_trades,
            DATE_DIFF('day', m.created_at::DATE, m.end_date::DATE)         AS duration_days
        FROM read_parquet('{MARKETS}') m
        JOIN last_prices lp ON lp.market_id = m.id
        WHERE m.closed = 1
          AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
          AND lp.nb_trades >= 3
        ORDER BY m.end_date
    """).df()

    logger.info(f"Marchés BTC Up/Down trouvés : {len(df):,}")
    if len(df) == 0:
        return df

    # Analyse descriptive
    logger.info(f"  Outcome YES wins : {(df['outcome']==1).sum()} ({(df['outcome']==1).mean()*100:.1f}%)")
    logger.info(f"  Outcome NO wins  : {(df['outcome']==0).sum()} ({(df['outcome']==0).mean()*100:.1f}%)")
    logger.info(f"  Prix YES moyen   : {df['avg_price_yes'].mean():.3f}")
    logger.info(f"  Volume médian    : {df['volume'].median():.0f}$")
    logger.info(f"  Durée médiane    : {df['duration_days'].median():.0f} jours")

    logger.info("\nExemples de marchés :")
    for _, row in df.head(8).iterrows():
        logger.info(f"  [{row['outcome']}] {row['question'][:70]:70s} | last_YES={row['last_price_yes']:.3f}")

    df.to_csv(OUT_DIR / "btc_markets_base.csv", index=False)
    return df


# ── 2. Exploration des patterns de prix Polymarket ────────────────────────────

def explore_price_patterns(df: pd.DataFrame):
    """
    Analyse la relation entre le prix YES sur Polymarket et la résolution réelle.
    - Calibration : le marché est-il bien calibré (50% YES → 50% de wins) ?
    - Drift : le prix change-t-il dans les 6h avant résolution ?
    """
    logger.info("\n=== 2. Exploration des patterns de prix ===")

    # Calibration par bin de prix
    df2 = df.dropna(subset=["last_price_yes", "outcome"]).copy()
    df2["price_bin"] = pd.cut(df2["last_price_yes"], bins=10)
    calib = df2.groupby("price_bin").agg(
        nb=("outcome", "count"),
        yes_wins=("outcome", "sum"),
        avg_price=("last_price_yes", "mean"),
    ).reset_index()
    calib["win_rate"] = calib["yes_wins"] / calib["nb"]
    calib["ecart"]    = calib["win_rate"] - calib["avg_price"]  # sur/sous-estimation

    logger.info("\nCalibration (prix YES Polymarket vs win rate réel) :")
    logger.info(f"  {'Bin de prix':25s} {'N':>5} {'Prix moy':>9} {'WR réel':>9} {'Écart':>8}")
    for _, row in calib.iterrows():
        logger.info(f"  {str(row['price_bin']):25s} {row['nb']:>5} "
                    f"{row['avg_price']:>8.3f} {row['win_rate']:>8.3f} {row['ecart']:>+8.3f}")

    calib.to_csv(OUT_DIR / "price_calibration.csv", index=False)

    # Drift : prix 6-24h avant résolution vs 0-6h avant
    df3 = df.dropna(subset=["price_last_6h", "price_6_24h"]).copy()
    df3["drift"] = df3["price_last_6h"] - df3["price_6_24h"]
    logger.info(f"\nDrift de prix (6h finales vs 6-24h avant) :")
    logger.info(f"  Drift moyen     : {df3['drift'].mean():+.4f}")
    logger.info(f"  Drift médian    : {df3['drift'].median():+.4f}")
    logger.info(f"  % qui monte     : {(df3['drift'] > 0).mean()*100:.1f}%")
    logger.info(f"  % qui baisse    : {(df3['drift'] < 0).mean()*100:.1f}%")

    # Corrélation drift → résolution
    drift_up   = df3[df3["drift"] > 0.05]
    drift_down = df3[df3["drift"] < -0.05]
    if len(drift_up) > 5:
        logger.info(f"  Marchés qui montent (drift>5%) : WR YES={drift_up['outcome'].mean()*100:.1f}% (n={len(drift_up)})")
    if len(drift_down) > 5:
        logger.info(f"  Marchés qui baissent (drift<-5%) : WR YES={drift_down['outcome'].mean()*100:.1f}% (n={len(drift_down)})")

    # Visualisation calibration
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle("BTC Up/Down — Calibration du marché Polymarket", fontsize=12)

    axes[0].scatter(calib["avg_price"], calib["win_rate"],
                    s=calib["nb"]*3, color="steelblue", alpha=0.7)
    axes[0].plot([0, 1], [0, 1], "r--", label="Calibration parfaite")
    axes[0].set_xlabel("Prix YES Polymarket")
    axes[0].set_ylabel("Win rate réel (YES wins)")
    axes[0].set_title("Calibration")
    axes[0].legend()
    for _, row in calib.iterrows():
        axes[0].annotate(f"n={row['nb']}", (row["avg_price"], row["win_rate"]),
                         textcoords="offset points", xytext=(5, 3), fontsize=7)

    axes[1].hist(df3["drift"], bins=30, color="coral", edgecolor="white")
    axes[1].axvline(0, color="black", linewidth=1)
    axes[1].set_xlabel("Drift de prix (6h finales - 6-24h avant)")
    axes[1].set_title("Distribution du drift de prix")

    plt.tight_layout()
    save_fig(fig, "btc_price_calibration")

    return df2, df3


# ── 3. Récupération des prix BTC Binance sur les fenêtres des marchés ─────────

def fetch_btc_prices_for_markets(df: pd.DataFrame) -> pd.DataFrame:
    """
    Pour chaque marché BTC Up/Down, récupère le mouvement BTC réel :
      - Sur les 6h avant résolution (bougies 5m)
      - Sur les 24h avant résolution

    Retourne df enrichi de colonnes btc_ret_6h, btc_ret_24h, btc_vol_6h.
    """
    logger.info("\n=== 3. Récupération des prix BTC Binance ===")

    df = df.copy()
    df["end_date_dt"] = pd.to_datetime(df["end_date"], utc=True, errors="coerce")
    df = df.dropna(subset=["end_date_dt"])

    # Limiter à 500 marchés les plus liquides pour éviter 40K appels API
    if len(df) > 500:
        df = df.nlargest(500, "volume").copy()
        logger.info(f"  Échantillon limité aux 500 marchés les plus liquides")

    # Grouper les marchés par semaine pour limiter les appels API
    df["week"] = df["end_date_dt"].dt.to_period("W").astype(str)
    weeks = df["week"].unique()
    logger.info(f"  {len(df)} marchés répartis sur {len(weeks)} semaines")

    # Télécharger les klines par période d'un mois (moins d'appels)
    btc_data_cache: dict[str, pd.DataFrame] = {}

    def _get_week_data(end_dt: pd.Timestamp) -> Optional[pd.DataFrame]:
        """Retourne 48h de klines 5m avant end_dt."""
        start = end_dt - timedelta(hours=48)
        month_key = end_dt.strftime("%Y-%m")
        if month_key not in btc_data_cache:
            month_start = end_dt.replace(day=1, hour=0, minute=0, second=0)
            month_end   = (month_start + timedelta(days=35)).replace(day=1)
            kl = get_klines_range(month_start.to_pydatetime(),
                                  month_end.to_pydatetime(), interval="5m")
            btc_data_cache[month_key] = kl
        kl = btc_data_cache[month_key]
        if kl is None:
            return None
        mask = (kl["open_time"] >= start) & (kl["open_time"] <= end_dt)
        return kl[mask].copy()

    btc_ret_6h, btc_ret_24h, btc_vol_6h = [], [], []
    btc_price_at_end, btc_price_6h_ago = [], []

    for _, row in df.iterrows():
        end_dt = row["end_date_dt"]
        kl = _get_week_data(end_dt)

        if kl is None or len(kl) < 5:
            btc_ret_6h.append(np.nan)
            btc_ret_24h.append(np.nan)
            btc_vol_6h.append(np.nan)
            btc_price_at_end.append(np.nan)
            btc_price_6h_ago.append(np.nan)
            continue

        kl = compute_indicators(kl)

        # Prix au moment de la résolution (dernière bougie avant end_dt)
        p_end = kl.iloc[-1]["close"]

        # Prix il y a 6h (72 bougies de 5min = 6h)
        idx_6h  = max(0, len(kl) - 72)
        idx_24h = max(0, len(kl) - 288)

        p_6h  = kl.iloc[idx_6h]["close"]
        p_24h = kl.iloc[idx_24h]["close"]

        ret_6h  = (p_end - p_6h)  / p_6h  * 100 if p_6h  > 0 else np.nan
        ret_24h = (p_end - p_24h) / p_24h * 100 if p_24h > 0 else np.nan

        # Volatilité réalisée sur les 6h (std des rendements 5m)
        vol_6h = kl.iloc[idx_6h:]["ret_1"].std() if len(kl) > idx_6h else np.nan

        btc_ret_6h.append(ret_6h)
        btc_ret_24h.append(ret_24h)
        btc_vol_6h.append(vol_6h)
        btc_price_at_end.append(p_end)
        btc_price_6h_ago.append(p_6h)

    df["btc_ret_6h"]       = btc_ret_6h
    df["btc_ret_24h"]      = btc_ret_24h
    df["btc_vol_6h"]       = btc_vol_6h
    df["btc_price_at_end"] = btc_price_at_end
    df["btc_price_6h_ago"] = btc_price_6h_ago

    n_ok = df["btc_ret_6h"].notna().sum()
    logger.info(f"  Marchés avec données BTC : {n_ok}/{len(df)}")
    df.to_csv(OUT_DIR / "btc_markets_enriched.csv", index=False)
    return df


# ── 4. Stratégie SA — Contre-momentum ─────────────────────────────────────────

def strategy_sa_counter_momentum(df: pd.DataFrame, threshold_pct: float = 0.5) -> pd.DataFrame:
    """
    SA : Si BTC a monté de +X% dans les 6h avant résolution → parier NO (DOWN).
    Si BTC a baissé de -X% → parier YES (UP).

    Logique : les mouvements courts tendent à se corriger (mean reversion).

    threshold_pct : seuil minimal de mouvement pour déclencher le signal (%)
    """
    df2 = df.dropna(subset=["btc_ret_6h", "last_price_yes", "outcome"]).copy()

    # Signal contre-momentum
    df2["sa_signal"] = np.where(
        df2["btc_ret_6h"] > threshold_pct,  "bet_NO",    # BTC montait → parier NO
        np.where(
        df2["btc_ret_6h"] < -threshold_pct, "bet_YES",   # BTC baissait → parier YES
        "no_trade"))

    results = []
    for signal in ["bet_NO", "bet_YES"]:
        sub = df2[df2["sa_signal"] == signal]
        if len(sub) == 0:
            continue
        # bet_NO → on gagne si outcome=0, bet_YES → on gagne si outcome=1
        wins = (sub["outcome"] == (1 if signal == "bet_YES" else 0))
        results.append({
            "signal":    signal,
            "n":         len(sub),
            "wins":      wins.sum(),
            "win_rate":  wins.mean() * 100,
            "btc_ret_mean": sub["btc_ret_6h"].mean(),
        })

    return df2, pd.DataFrame(results)


# ── 5. Stratégie SB — Momentum ────────────────────────────────────────────────

def strategy_sb_momentum(df: pd.DataFrame, threshold_pct: float = 0.5) -> pd.DataFrame:
    """
    SB : Si BTC a monté → parier YES (continuation de la tendance).
    Si BTC a baissé → parier NO.
    """
    df2 = df.dropna(subset=["btc_ret_6h", "outcome"]).copy()

    df2["sb_signal"] = np.where(
        df2["btc_ret_6h"] > threshold_pct,  "bet_YES",
        np.where(
        df2["btc_ret_6h"] < -threshold_pct, "bet_NO",
        "no_trade"))

    results = []
    for signal in ["bet_YES", "bet_NO"]:
        sub = df2[df2["sb_signal"] == signal]
        if len(sub) == 0:
            continue
        wins = (sub["outcome"] == (1 if signal == "bet_YES" else 0))
        results.append({
            "signal":    signal,
            "n":         len(sub),
            "wins":      wins.sum(),
            "win_rate":  wins.mean() * 100,
            "btc_ret_mean": sub["btc_ret_6h"].mean(),
        })

    return df2, pd.DataFrame(results)


# ── 6. Stratégie SC — Mispricing ──────────────────────────────────────────────

def strategy_sc_mispricing(df: pd.DataFrame) -> pd.DataFrame:
    """
    SC : Exploiter l'écart entre le prix Polymarket et le signal BTC réel.

    Si BTC monte fort (+X%) mais Polymarket sous-évalue YES (UP) → parier YES.
    Si BTC baisse fort (-X%) mais Polymarket sur-évalue YES → parier NO.

    On cherche les cas où le marché est en retard sur la réalité BTC.
    """
    df2 = df.dropna(subset=["btc_ret_6h", "last_price_yes", "outcome"]).copy()

    # "Fair" probability approximative basée sur le retour BTC
    # Si BTC fait +2% en 6h, la probabilité que le marché résout "UP" devrait être > 50%
    # On utilise une sigmoid pour mapper le rendement BTC → probabilité
    def btc_to_fair_prob(ret_pct):
        # sigmoid centrée en 0, pente = 5 (1% de mouvement → ~12pp de probabilité)
        return 1 / (1 + np.exp(-5 * ret_pct / 100))

    df2["fair_prob_yes"] = df2["btc_ret_6h"].apply(btc_to_fair_prob)
    df2["mispricing"]    = df2["fair_prob_yes"] - df2["last_price_yes"]
    # Positif → Polymarket sous-évalue YES par rapport au signal BTC
    # Négatif → Polymarket sur-évalue YES

    # Seuils de signal
    MISPRICE_THRESHOLD = 0.15  # 15% d'écart pour déclencher
    df2["sc_signal"] = np.where(
        df2["mispricing"] > MISPRICE_THRESHOLD,   "bet_YES",  # Sous-évalué → acheter YES
        np.where(
        df2["mispricing"] < -MISPRICE_THRESHOLD,  "bet_NO",   # Sur-évalué → vendre YES
        "no_trade"))

    results = []
    for signal in ["bet_YES", "bet_NO", "no_trade"]:
        sub = df2[df2["sc_signal"] == signal]
        if len(sub) == 0:
            continue
        if signal == "no_trade":
            wins = pd.Series(dtype=bool)
        else:
            wins = (sub["outcome"] == (1 if signal == "bet_YES" else 0))
        results.append({
            "signal":          signal,
            "n":               len(sub),
            "wins":            wins.sum() if len(wins) > 0 else np.nan,
            "win_rate":        wins.mean() * 100 if len(wins) > 0 else np.nan,
            "mispricing_mean": sub["mispricing"].mean(),
        })

    return df2, pd.DataFrame(results)


# ── 7. Stratégie SD — Arbitrage somme < 1 ─────────────────────────────────────

def strategy_sd_sum_arbitrage(con) -> pd.DataFrame:
    """
    SD : Si YES_price + NO_price < 0.98 (couvre les frais 2%), acheter les deux.
    Profit garanti : 1 - (YES + NO) - frais.

    On cherche des snapshots temporels où le carnet d'ordres Polymarket
    offre YES + NO < 0.98.
    """
    logger.info("\n=== Stratégie SD : Arbitrage somme < 1 ===")

    df = con.execute(f"""
        WITH market_snapshots AS (
            -- Pour chaque marché et chaque heure, calcule le prix YES moyen
            -- et estime le prix NO = 1 - prix_YES (sur les marchés binaires)
            SELECT
                q.market_id,
                DATE_TRUNC('hour', epoch_ms(q.timestamp::BIGINT*1000))     AS heure,
                AVG(CASE WHEN q.side='BUY'  THEN q.price END)              AS bid_yes,
                AVG(CASE WHEN q.side='SELL' THEN q.price END)              AS ask_yes,
                -- Prix NO estimé depuis les trades
                AVG(q.price)                                                AS mid_yes,
                1 - AVG(q.price)                                            AS mid_no,
                AVG(q.price) + (1 - AVG(q.price))                          AS sum_yes_no,
                COUNT(*)                                                     AS nb_trades
            FROM read_parquet('{QUANT}') q
            JOIN read_parquet('{MARKETS}') m ON m.id = q.market_id
            WHERE (LOWER(m.question) LIKE '%up or down%'
                   OR LOWER(m.question) LIKE '%updown%')
              AND (LOWER(m.question) LIKE '%bitcoin%'
                   OR LOWER(m.question) LIKE '% btc%')
              AND m.closed = 0  -- Marchés encore ouverts dans l'historique
            GROUP BY q.market_id, DATE_TRUNC('hour', epoch_ms(q.timestamp::BIGINT*1000))
            HAVING COUNT(*) >= 3
        )
        SELECT
            market_id,
            heure,
            ROUND(mid_yes, 4) AS mid_yes,
            ROUND(mid_no, 4)  AS mid_no,
            ROUND(bid_yes, 4) AS bid_yes,
            ROUND(ask_yes, 4) AS ask_yes,
            nb_trades,
            -- Profit brut si on achète YES + NO (en unités de $100 investi)
            ROUND((1 - mid_yes - mid_no) * 100, 2) AS profit_brut_pct,
            -- Profit net après frais 2%
            ROUND((1 - mid_yes - mid_no - 0.02) * 100, 2) AS profit_net_pct
        FROM market_snapshots
        -- Anomalie : sum > 1 (normal) ou sum < 1 (opportunité d'arbitrage)
        ORDER BY sum_yes_no ASC
        LIMIT 100
    """).df()

    if len(df) == 0:
        logger.warning("Aucune opportunité d'arbitrage SD trouvée")
        return df

    arb_opportunities = df[df["profit_net_pct"] > 0]
    logger.info(f"  Snapshots analysés : {len(df):,}")
    logger.info(f"  Opportunités d'arb (profit net > 0) : {len(arb_opportunities)}")

    if len(arb_opportunities) > 0:
        logger.info(f"  Profit net moyen : {arb_opportunities['profit_net_pct'].mean():.2f}%")
        logger.info(f"  Profit net max   : {arb_opportunities['profit_net_pct'].max():.2f}%")
        logger.info("\n  Top opportunités :")
        logger.info(arb_opportunities.head(10).to_string(index=False))

    df.to_csv(OUT_DIR / "sd_arbitrage.csv", index=False)
    return df


# ── 8. Scan par seuil de momentum ─────────────────────────────────────────────

def scan_thresholds(df: pd.DataFrame):
    """
    Teste SA et SB sur plusieurs seuils de momentum (0.1% à 2.0%)
    pour trouver le seuil optimal.
    """
    logger.info("\n=== Scan des seuils momentum (SA vs SB) ===")
    thresholds = [0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0]
    rows = []

    for t in thresholds:
        df_sa, res_sa = strategy_sa_counter_momentum(df, threshold_pct=t)
        df_sb, res_sb = strategy_sb_momentum(df, threshold_pct=t)

        for signal in ["bet_NO", "bet_YES"]:
            sa_row = res_sa[res_sa["signal"] == signal]
            sb_row = res_sb[res_sb["signal"] == signal]
            if len(sa_row) > 0:
                rows.append({"strat": "SA", "threshold": t, "signal": signal,
                             "n": sa_row["n"].values[0],
                             "wr": sa_row["win_rate"].values[0]})
            if len(sb_row) > 0:
                rows.append({"strat": "SB", "threshold": t, "signal": signal,
                             "n": sb_row["n"].values[0],
                             "wr": sb_row["win_rate"].values[0]})

    result = pd.DataFrame(rows)
    if result.empty:
        return result

    logger.info(f"\n{'Strat':4s} {'Signal':8s} {'Seuil':7s} {'N':>6} {'WR%':>8}")
    logger.info("─" * 40)
    for _, row in result.iterrows():
        logger.info(f"{row['strat']:4s} {row['signal']:8s} {row['threshold']:>6.2f}% "
                    f"{row['n']:>6} {row['wr']:>7.1f}%")

    result.to_csv(OUT_DIR / "threshold_scan.csv", index=False)
    return result


# ── 9. Visualisation ──────────────────────────────────────────────────────────

def plot_results(df_enriched: pd.DataFrame, threshold_scan: pd.DataFrame):
    """Graphiques synthétiques."""

    df = df_enriched.dropna(subset=["btc_ret_6h", "outcome"]).copy()
    if len(df) < 10:
        logger.warning("Pas assez de données pour les graphiques")
        return

    fig, axes = plt.subplots(2, 2, figsize=(16, 11))
    fig.suptitle("BTC Up/Down — Analyse Polymarket vs Prix réel", fontsize=14)

    # ── A : Distribution des rendements BTC 6h avant résolution ─────────────
    ax = axes[0][0]
    yes_wins = df[df["outcome"] == 1]["btc_ret_6h"]
    no_wins  = df[df["outcome"] == 0]["btc_ret_6h"]
    ax.hist(yes_wins, bins=30, alpha=0.6, color="green", label=f"YES wins (n={len(yes_wins)})")
    ax.hist(no_wins,  bins=30, alpha=0.6, color="red",   label=f"NO wins (n={len(no_wins)})")
    ax.axvline(0, color="black", linewidth=1)
    ax.set_xlabel("Rendement BTC 6h avant résolution (%)")
    ax.set_ylabel("Fréquence")
    ax.set_title("BTC ret 6h selon la résolution du marché")
    ax.legend()

    # ── B : Prix YES vs résolution (nuage de points) ─────────────────────────
    ax = axes[0][1]
    jitter = np.random.default_rng(42).uniform(-0.02, 0.02, len(df))
    colors = df["outcome"].map({0: "red", 1: "green"})
    ax.scatter(df["last_price_yes"], df["outcome"] + jitter,
               c=colors, alpha=0.3, s=10)
    ax.set_xlabel("Dernier prix YES Polymarket")
    ax.set_ylabel("Résolution (0=NO, 1=YES)")
    ax.set_title("Prix YES Polymarket vs Résolution")

    # ── C : Scatter BTC ret vs Polymarket YES price ──────────────────────────
    ax = axes[1][0]
    ax.scatter(df["btc_ret_6h"], df["last_price_yes"],
               c=df["outcome"].map({0:"red", 1:"green"}), alpha=0.4, s=15)
    ax.axhline(0.5, color="gray", linestyle="--", label="YES = 50%")
    ax.axvline(0,   color="gray", linestyle="--", label="BTC ret = 0")
    ax.set_xlabel("Rendement BTC 6h avant résolution (%)")
    ax.set_ylabel("Prix YES Polymarket")
    ax.set_title("Mispricing : BTC ret vs Polymarket YES\n(vert=YES gagne, rouge=NO gagne)")
    ax.legend(fontsize=8)

    # ── D : Win rate SA vs SB par seuil ─────────────────────────────────────
    ax = axes[1][1]
    if not threshold_scan.empty:
        for strat, color, marker in [("SA", "steelblue", "o"), ("SB", "coral", "s")]:
            sub = threshold_scan[threshold_scan["strat"] == strat]
            # Moyenne sur les deux signaux (bet_YES et bet_NO)
            agg = sub.groupby("threshold")["wr"].mean()
            ax.plot(agg.index, agg.values, marker=marker, color=color,
                    label=strat, linewidth=2)
        ax.axhline(50, color="black", linestyle="--", label="50% (aléatoire)")
        ax.set_xlabel("Seuil momentum BTC (%)")
        ax.set_ylabel("Win rate moyen (%)")
        ax.set_title("SA (contre-momentum) vs SB (momentum)\nselon le seuil de déclenchement")
        ax.legend()

    plt.tight_layout()
    save_fig(fig, "btc_updown_analysis")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    log_file = setup()
    logger.info("=" * 65)
    logger.info("PHASE 3 — Edge BTC Up/Down : Polymarket vs Prix réel")
    logger.info("=" * 65)

    con = duckdb.connect()
    con.execute("SET threads = 4; SET memory_limit = '4GB'")

    # 1. Chargement des marchés
    df = load_btc_updown_markets(con)

    if len(df) == 0:
        logger.error("Aucun marché BTC Up/Down trouvé. Arrêt.")
        con.close()
        return

    # 2. Exploration des patterns de prix Polymarket seul
    df_calib, df_drift = explore_price_patterns(df)

    # 3. Enrichissement avec les prix BTC réels (Binance)
    logger.info("\n=== 3. Enrichissement avec les prix BTC (Binance API) ===")
    logger.info("  (peut prendre quelques minutes — données téléchargées et cachées)")
    df_enriched = fetch_btc_prices_for_markets(df)

    n_btc = df_enriched["btc_ret_6h"].notna().sum()
    threshold_scan = pd.DataFrame()

    if n_btc < 10:
        logger.warning(f"Trop peu de données BTC ({n_btc}) — stratégies SA/SB/SC ignorées")
        logger.info("  => Seule la stratégie SD (arbitrage somme) sera testée")
    else:
        logger.info(f"  {n_btc} marchés enrichis avec données BTC")

        # 4. Stratégie SA — Contre-momentum
        logger.info("\n=== 4. Stratégie SA — Contre-momentum ===")
        df_sa, res_sa = strategy_sa_counter_momentum(df_enriched, threshold_pct=0.5)
        logger.info(res_sa.to_string(index=False))
        res_sa.to_csv(OUT_DIR / "sa_results.csv", index=False)

        # 5. Stratégie SB — Momentum
        logger.info("\n=== 5. Stratégie SB — Momentum ===")
        df_sb, res_sb = strategy_sb_momentum(df_enriched, threshold_pct=0.5)
        logger.info(res_sb.to_string(index=False))
        res_sb.to_csv(OUT_DIR / "sb_results.csv", index=False)

        # 6. Stratégie SC — Mispricing
        logger.info("\n=== 6. Stratégie SC — Mispricing ===")
        df_sc, res_sc = strategy_sc_mispricing(df_enriched)
        logger.info(res_sc.to_string(index=False))
        res_sc.to_csv(OUT_DIR / "sc_results.csv", index=False)

        # 7. Scan des seuils
        threshold_scan = scan_thresholds(df_enriched)

    # 8. Arbitrage SD (sans données BTC)
    df_sd = strategy_sd_sum_arbitrage(con)

    # 9. Graphiques
    logger.info("\n=== Graphiques ===")
    plot_results(df_enriched, threshold_scan if 'threshold_scan' in dir() else pd.DataFrame())

    con.close()
    logger.info(f"\nOutputs : {OUT_DIR.relative_to(PROJECT_ROOT)}")
    logger.success("Analyse BTC Up/Down terminée.")


if __name__ == "__main__":
    main()
