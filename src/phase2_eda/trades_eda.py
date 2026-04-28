"""
Phase 2 — EDA : Analyse exploratoire des trades (quant.parquet)
================================================================
568 millions de trades, 35 GB — tout via DuckDB (jamais chargé en RAM).

Questions clés :
  1. Distribution des prix : y a-t-il un biais global ?
  2. BUY vs SELL : déséquilibre ?
  3. Volume dans le temps : quand le marché est-il actif ?
  4. Distribution des montants : qui sont les whales ?
  5. Top marchés par activité (nb trades + volume)
  6. Top wallets (makers/takers les plus actifs)
  7. Durée de vie des marchés vs volume : corrélation ?

Usage :
    python src/phase2_eda/trades_eda.py
"""

import sys
from pathlib import Path
from datetime import datetime

import duckdb
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from loguru import logger

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# =============================================================================
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
OUT_DIR      = PROJECT_ROOT / "outputs" / "phase2" / "trades"
# =============================================================================


def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_trades_eda.log"
    logger.remove()
    logger.add(sys.stdout, colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add(log_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
    return log_file


def save_fig(fig, name: str):
    path = OUT_DIR / f"{name}.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.success(f"Graphique sauvegarde : {path.relative_to(PROJECT_ROOT)}")


# ── 1. Vue d'ensemble ─────────────────────────────────────────────────────────
def overview(con, parquet: str):
    logger.info("=== 1. Vue d'ensemble des trades ===")
    df = con.execute(f"""
        SELECT
            COUNT(*)                                    AS total_trades,
            COUNT(DISTINCT market_id)                   AS nb_marches_actifs,
            COUNT(DISTINCT maker)                       AS nb_makers,
            COUNT(DISTINCT taker)                       AS nb_takers,
            ROUND(SUM(usd_amount) / 1e9, 3)            AS volume_total_G_usd,
            ROUND(AVG(usd_amount), 2)                   AS montant_moyen_usd,
            ROUND(MEDIAN(usd_amount), 2)                AS montant_median_usd,
            ROUND(AVG(price), 4)                        AS prix_moyen,
            ROUND(STDDEV(price), 4)                     AS prix_std,
            epoch_ms(MIN(timestamp)::BIGINT * 1000)::DATE       AS premier_trade,
            epoch_ms(MAX(timestamp)::BIGINT * 1000)::DATE       AS dernier_trade
        FROM read_parquet('{parquet}')
    """).df()

    for col in df.columns:
        logger.info(f"  {col:30s} : {df[col].iloc[0]}")
    return df


# ── 2. Distribution des prix ──────────────────────────────────────────────────
def price_distribution(con, parquet: str):
    logger.info("\n=== 2. Distribution des prix ===")

    # Histogramme en 20 buckets de 0.05
    hist = con.execute(f"""
        SELECT
            ROUND(FLOOR(price / 0.05) * 0.05, 2)   AS bin_prix,
            COUNT(*)                                  AS nb_trades,
            ROUND(SUM(usd_amount) / 1e6, 2)          AS volume_M_usd
        FROM read_parquet('{parquet}')
        WHERE price BETWEEN 0 AND 1
        GROUP BY 1
        ORDER BY 1
    """).df()

    hist.to_csv(OUT_DIR / "price_distribution.csv", index=False)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle("Distribution des prix (perspective YES)", fontsize=13)

    axes[0].bar(hist["bin_prix"].astype(str), hist["nb_trades"], color="steelblue", width=0.8)
    axes[0].set_xlabel("Prix (tranches de 0.05)")
    axes[0].set_ylabel("Nombre de trades")
    axes[0].set_title("Fréquence par niveau de prix")
    axes[0].tick_params(axis="x", rotation=90)
    axes[0].yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x/1e6:.1f}M"))

    axes[1].bar(hist["bin_prix"].astype(str), hist["volume_M_usd"], color="coral", width=0.8)
    axes[1].set_xlabel("Prix (tranches de 0.05)")
    axes[1].set_ylabel("Volume (M$)")
    axes[1].set_title("Volume par niveau de prix")
    axes[1].tick_params(axis="x", rotation=90)

    plt.tight_layout()
    save_fig(fig, "price_distribution")

    # Statistiques clés
    stats = con.execute(f"""
        SELECT
            ROUND(AVG(price), 4)                    AS prix_moyen,
            ROUND(MEDIAN(price), 4)                 AS prix_median,
            COUNT(*) FILTER (WHERE price < 0.1)     AS trades_prix_inf_10pct,
            COUNT(*) FILTER (WHERE price > 0.9)     AS trades_prix_sup_90pct,
            COUNT(*) FILTER (WHERE price BETWEEN 0.45 AND 0.55) AS trades_autour_50pct
        FROM read_parquet('{parquet}')
    """).df()
    logger.info(stats.to_string(index=False))


# ── 3. BUY vs SELL ────────────────────────────────────────────────────────────
def buy_sell_analysis(con, parquet: str):
    logger.info("\n=== 3. Analyse BUY vs SELL ===")

    bs = con.execute(f"""
        SELECT
            side,
            COUNT(*)                            AS nb_trades,
            ROUND(SUM(usd_amount) / 1e9, 3)    AS volume_G_usd,
            ROUND(AVG(price), 4)                AS prix_moyen,
            ROUND(AVG(usd_amount), 2)           AS montant_moyen_usd
        FROM read_parquet('{parquet}')
        GROUP BY side
        ORDER BY nb_trades DESC
    """).df()

    logger.info(bs.to_string(index=False))
    bs.to_csv(OUT_DIR / "buy_sell.csv", index=False)

    fig, axes = plt.subplots(1, 2, figsize=(10, 5))
    fig.suptitle("Déséquilibre BUY vs SELL", fontsize=13)

    colors = {"BUY": "green", "SELL": "red"}
    bar_colors = [colors.get(s, "gray") for s in bs["side"]]

    axes[0].bar(bs["side"], bs["nb_trades"], color=bar_colors)
    axes[0].set_ylabel("Nombre de trades")
    axes[0].set_title("Nb trades par côté")
    axes[0].yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x/1e6:.0f}M"))
    for i, n in enumerate(bs["nb_trades"]):
        axes[0].text(i, n * 1.01, f"{n/1e6:.1f}M", ha="center")

    axes[1].bar(bs["side"], bs["volume_G_usd"], color=bar_colors)
    axes[1].set_ylabel("Volume (G$)")
    axes[1].set_title("Volume par côté")
    for i, v in enumerate(bs["volume_G_usd"]):
        axes[1].text(i, v * 1.01, f"{v:.2f}G$", ha="center")

    plt.tight_layout()
    save_fig(fig, "buy_sell")


# ── 4. Volume dans le temps ───────────────────────────────────────────────────
def temporal_volume(con, parquet: str):
    logger.info("\n=== 4. Volume dans le temps ===")

    monthly = con.execute(f"""
        SELECT
            DATE_TRUNC('month', epoch_ms(timestamp::BIGINT * 1000))::DATE   AS mois,
            COUNT(*)                                                  AS nb_trades,
            ROUND(SUM(usd_amount) / 1e6, 2)                         AS volume_M_usd
        FROM read_parquet('{parquet}')
        GROUP BY 1
        ORDER BY 1
    """).df()

    monthly.to_csv(OUT_DIR / "volume_mensuel.csv", index=False)
    logger.info(f"Periode : {monthly['mois'].iloc[0]} → {monthly['mois'].iloc[-1]}")
    logger.info(f"Mois avec le plus de volume : {monthly.loc[monthly['volume_M_usd'].idxmax(), 'mois']}")

    fig, ax1 = plt.subplots(figsize=(14, 5))
    ax2 = ax1.twinx()

    ax1.bar(monthly["mois"].astype(str), monthly["nb_trades"], color="steelblue", alpha=0.6, label="Nb trades")
    ax2.plot(monthly["mois"].astype(str), monthly["volume_M_usd"], color="coral", linewidth=2, label="Volume M$")

    ax1.set_ylabel("Nb trades", color="steelblue")
    ax2.set_ylabel("Volume (M$)", color="coral")
    ax1.set_title("Volume et nombre de trades par mois")

    ticks = list(range(0, len(monthly), max(1, len(monthly) // 12)))
    ax1.set_xticks(ticks)
    ax1.set_xticklabels([monthly["mois"].astype(str).iloc[i] for i in ticks], rotation=45, ha="right")
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x/1e6:.0f}M"))

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    plt.tight_layout()
    save_fig(fig, "volume_mensuel")


# ── 5. Distribution des montants (whales) ─────────────────────────────────────
def amount_distribution(con, parquet: str):
    logger.info("\n=== 5. Distribution des montants (detection whales) ===")

    buckets = con.execute(f"""
        SELECT
            CASE
                WHEN usd_amount < 10       THEN '1. < 10$'
                WHEN usd_amount < 100      THEN '2. 10-100$'
                WHEN usd_amount < 1000     THEN '3. 100$-1K$'
                WHEN usd_amount < 10000    THEN '4. 1K-10K$'
                WHEN usd_amount < 100000   THEN '5. 10K-100K$'
                ELSE                            '6. > 100K$ (whale)'
            END                                AS tranche,
            COUNT(*)                           AS nb_trades,
            ROUND(SUM(usd_amount) / 1e6, 2)   AS volume_M_usd,
            ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct_trades
        FROM read_parquet('{parquet}')
        GROUP BY 1
        ORDER BY 1
    """).df()

    logger.info(buckets.to_string(index=False))
    buckets.to_csv(OUT_DIR / "amount_distribution.csv", index=False)

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    fig.suptitle("Distribution des montants par trade", fontsize=13)

    axes[0].barh(buckets["tranche"], buckets["nb_trades"], color="steelblue")
    axes[0].set_xlabel("Nombre de trades")
    axes[0].set_title("Nb trades par tranche")
    axes[0].xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x/1e6:.0f}M"))

    axes[1].barh(buckets["tranche"], buckets["volume_M_usd"], color="coral")
    axes[1].set_xlabel("Volume total (M$)")
    axes[1].set_title("Volume par tranche (qui domine ?)")

    plt.tight_layout()
    save_fig(fig, "amount_distribution")


# ── 6. Top marchés par activité ───────────────────────────────────────────────
def top_markets_by_trades(con, parquet: str, markets_parquet: str):
    logger.info("\n=== 6. Top 20 marches par activite ===")

    top = con.execute(f"""
        SELECT
            q.market_id,
            m.question,
            COUNT(*)                            AS nb_trades,
            ROUND(SUM(q.usd_amount) / 1e6, 2)  AS volume_M_usd,
            ROUND(AVG(q.price), 3)              AS prix_moyen
        FROM read_parquet('{parquet}') q
        LEFT JOIN read_parquet('{markets_parquet}') m ON q.market_id = m.id
        GROUP BY q.market_id, m.question
        ORDER BY nb_trades DESC
        LIMIT 20
    """).df()

    top["question_court"] = top["question"].apply(lambda q: (q[:55] + "…") if q and len(q) > 55 else q)
    logger.info(top[["market_id", "question_court", "nb_trades", "volume_M_usd"]].to_string(index=False))
    top.to_csv(OUT_DIR / "top20_marches_actifs.csv", index=False)

    fig, ax = plt.subplots(figsize=(12, 8))
    ax.barh(top["question_court"][::-1], top["nb_trades"][::-1], color="steelblue")
    ax.set_xlabel("Nombre de trades")
    ax.set_title("Top 20 marchés par nombre de trades")
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x/1e6:.1f}M"))
    plt.tight_layout()
    save_fig(fig, "top20_marches_actifs")


# ── 7. Top wallets ────────────────────────────────────────────────────────────
def top_wallets(con, parquet: str):
    logger.info("\n=== 7. Top 20 wallets (makers) ===")

    top = con.execute(f"""
        SELECT
            maker                               AS wallet,
            COUNT(*)                            AS nb_trades,
            ROUND(SUM(usd_amount) / 1e6, 2)    AS volume_M_usd,
            ROUND(AVG(usd_amount), 2)           AS montant_moyen,
            COUNT(DISTINCT market_id)           AS nb_marches
        FROM read_parquet('{parquet}')
        GROUP BY maker
        ORDER BY volume_M_usd DESC
        LIMIT 20
    """).df()

    logger.info(top.to_string(index=False))
    top.to_csv(OUT_DIR / "top20_wallets.csv", index=False)

    fig, ax = plt.subplots(figsize=(12, 7))
    short_wallets = [w[:10] + "…" + w[-4:] for w in top["wallet"]]
    ax.barh(short_wallets[::-1], top["volume_M_usd"][::-1], color="steelblue")
    ax.set_xlabel("Volume total (M$)")
    ax.set_title("Top 20 wallets par volume")
    plt.tight_layout()
    save_fig(fig, "top20_wallets")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log_file = setup()
    parquet  = str(DATA_DIR / "quant.parquet")
    markets  = str(DATA_DIR / "markets.parquet")

    logger.info("=" * 60)
    logger.info("PHASE 2 — EDA : quant.parquet (568M trades, 35 GB)")
    logger.info("Toutes les requetes via DuckDB — RAM non surchargee")
    logger.info("=" * 60)

    con = duckdb.connect()
    # Optimisation DuckDB : utiliser plusieurs threads et limiter la RAM
    con.execute("SET threads = 4")
    con.execute("SET memory_limit = '4GB'")

    overview(con, parquet)
    price_distribution(con, parquet)
    buy_sell_analysis(con, parquet)
    temporal_volume(con, parquet)
    amount_distribution(con, parquet)
    top_markets_by_trades(con, parquet, markets)
    top_wallets(con, parquet)

    con.close()

    logger.info(f"\nOutputs sauvegardes dans : {OUT_DIR.relative_to(PROJECT_ROOT)}")
    logger.info(f"Log : {log_file}")
    logger.success("EDA trades terminee.")


if __name__ == "__main__":
    main()
