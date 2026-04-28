"""
Phase 2 — EDA : Analyse exploratoire des marchés (markets.parquet)
===================================================================
Questions clés :
  1. Combien de marchés sont exploitables (volume > 0, fermés) ?
  2. Quelles catégories dominent ? (crypto, politique, sport…)
  3. Distribution des volumes : où est la liquidité ?
  4. Patterns temporels : quand les marchés sont-ils créés / résolus ?
  5. Taux de résolution YES vs NO

Usage :
    python src/phase2_eda/markets_eda.py
"""

import sys
from pathlib import Path
from datetime import datetime

import duckdb
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # Pas d'affichage graphique — on sauvegarde en PNG
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from loguru import logger

# Forcer UTF-8 sur Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# =============================================================================
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
OUT_DIR      = PROJECT_ROOT / "outputs" / "phase2" / "markets"
# =============================================================================


def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_markets_eda.log"
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
def overview(con, parquet: str) -> pd.DataFrame:
    logger.info("=== 1. Vue d'ensemble ===")
    df = con.execute(f"""
        SELECT
            COUNT(*)                                        AS total_marches,
            SUM(closed)                                     AS fermes,
            SUM(active)                                     AS actifs,
            SUM(archived)                                   AS archives,
            SUM(neg_risk)                                   AS neg_risk,
            COUNT(DISTINCT event_id)                        AS nb_evenements,
            ROUND(SUM(volume) / 1e6, 2)                     AS volume_total_M_usd,
            ROUND(AVG(volume), 2)                           AS volume_moyen_usd,
            ROUND(MEDIAN(volume), 2)                        AS volume_median_usd,
            MIN(created_at)::DATE                           AS premier_marche,
            MAX(created_at)::DATE                           AS dernier_marche
        FROM read_parquet('{parquet}')
    """).df()

    for col in df.columns:
        logger.info(f"  {col:30s} : {df[col].iloc[0]}")
    return df


# ── 2. Marchés avec volume > 0 (exploitables) ────────────────────────────────
def volume_distribution(con, parquet: str):
    logger.info("\n=== 2. Distribution des volumes ===")

    # Tranches de volume
    buckets = con.execute(f"""
        SELECT
            CASE
                WHEN volume = 0                    THEN '0. Aucun volume'
                WHEN volume < 1000                 THEN '1. < 1K$'
                WHEN volume < 10000                THEN '2. 1K-10K$'
                WHEN volume < 100000               THEN '3. 10K-100K$'
                WHEN volume < 1000000              THEN '4. 100K-1M$'
                ELSE                                    '5. > 1M$'
            END                                    AS tranche,
            COUNT(*)                               AS nb_marches,
            ROUND(SUM(volume) / 1e6, 2)            AS volume_M_usd
        FROM read_parquet('{parquet}')
        GROUP BY 1
        ORDER BY 1
    """).df()

    logger.info(buckets.to_string(index=False))
    buckets.to_csv(OUT_DIR / "volume_distribution.csv", index=False)

    # Graphique
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle("Distribution des volumes — markets.parquet", fontsize=13)

    axes[0].barh(buckets["tranche"], buckets["nb_marches"], color="steelblue")
    axes[0].set_xlabel("Nombre de marchés")
    axes[0].set_title("Nb marchés par tranche de volume")
    axes[0].xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))

    axes[1].barh(buckets["tranche"], buckets["volume_M_usd"], color="coral")
    axes[1].set_xlabel("Volume total (M$)")
    axes[1].set_title("Volume total par tranche")

    plt.tight_layout()
    save_fig(fig, "volume_distribution")
    return buckets


# ── 3. Catégories de marchés ──────────────────────────────────────────────────
def market_categories(con, parquet: str):
    logger.info("\n=== 3. Catégories de marchés (analyse du champ 'question') ===")

    # On détecte les types via mots-clés dans la question
    cats = con.execute(f"""
        SELECT
            CASE
                WHEN LOWER(question) LIKE '%bitcoin%' OR LOWER(question) LIKE '% btc%'
                     OR LOWER(question) LIKE '%ethereum%' OR LOWER(question) LIKE '% eth%'
                     OR LOWER(question) LIKE '%crypto%' OR LOWER(question) LIKE '%solana%'
                     OR LOWER(question) LIKE '%xrp%' OR LOWER(question) LIKE '%up or down%'
                     THEN 'Crypto'
                WHEN LOWER(question) LIKE '%president%' OR LOWER(question) LIKE '%election%'
                     OR LOWER(question) LIKE '%democrat%' OR LOWER(question) LIKE '%republican%'
                     OR LOWER(question) LIKE '%trump%' OR LOWER(question) LIKE '%biden%'
                     OR LOWER(question) LIKE '%harris%' OR LOWER(question) LIKE '%senate%'
                     OR LOWER(question) LIKE '%congress%' OR LOWER(question) LIKE '%vote%'
                     THEN 'Politique'
                WHEN LOWER(question) LIKE '%nba%' OR LOWER(question) LIKE '%nfl%'
                     OR LOWER(question) LIKE '%soccer%' OR LOWER(question) LIKE '%football%'
                     OR LOWER(question) LIKE '%tennis%' OR LOWER(question) LIKE '%league%'
                     OR LOWER(question) LIKE '%championship%' OR LOWER(question) LIKE '%cup%'
                     THEN 'Sport'
                WHEN LOWER(question) LIKE '%fed%' OR LOWER(question) LIKE '%rate%'
                     OR LOWER(question) LIKE '%inflation%' OR LOWER(question) LIKE '%gdp%'
                     OR LOWER(question) LIKE '%recession%' OR LOWER(question) LIKE '%market%'
                     THEN 'Economie'
                ELSE 'Autre'
            END                                AS categorie,
            COUNT(*)                           AS nb_marches,
            ROUND(SUM(volume) / 1e6, 2)        AS volume_M_usd,
            ROUND(AVG(volume), 0)              AS volume_moyen_usd
        FROM read_parquet('{parquet}')
        GROUP BY 1
        ORDER BY nb_marches DESC
    """).df()

    logger.info(cats.to_string(index=False))
    cats.to_csv(OUT_DIR / "categories.csv", index=False)

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig.suptitle("Catégories de marchés", fontsize=13)

    axes[0].pie(cats["nb_marches"], labels=cats["categorie"], autopct="%1.1f%%", startangle=90)
    axes[0].set_title("Répartition par nombre de marchés")

    axes[1].pie(cats["volume_M_usd"], labels=cats["categorie"], autopct="%1.1f%%", startangle=90)
    axes[1].set_title("Répartition par volume ($)")

    plt.tight_layout()
    save_fig(fig, "categories")
    return cats


# ── 4. Distribution temporelle ────────────────────────────────────────────────
def temporal_analysis(con, parquet: str):
    logger.info("\n=== 4. Distribution temporelle des marchés ===")

    monthly = con.execute(f"""
        SELECT
            DATE_TRUNC('month', created_at)::DATE   AS mois,
            COUNT(*)                                 AS nb_marches,
            ROUND(SUM(volume) / 1e6, 2)             AS volume_M_usd
        FROM read_parquet('{parquet}')
        WHERE created_at IS NOT NULL
        GROUP BY 1
        ORDER BY 1
    """).df()

    monthly.to_csv(OUT_DIR / "temporal_monthly.csv", index=False)

    fig, ax1 = plt.subplots(figsize=(14, 5))
    ax2 = ax1.twinx()

    ax1.bar(monthly["mois"].astype(str), monthly["nb_marches"], color="steelblue", alpha=0.7, label="Nb marchés")
    ax2.plot(monthly["mois"].astype(str), monthly["volume_M_usd"], color="coral", linewidth=2, label="Volume M$")

    ax1.set_xlabel("Mois")
    ax1.set_ylabel("Nombre de marchés", color="steelblue")
    ax2.set_ylabel("Volume (M$)", color="coral")
    ax1.set_title("Création de marchés et volume par mois")

    ticks = list(range(0, len(monthly), max(1, len(monthly)//12)))
    ax1.set_xticks(ticks)
    ax1.set_xticklabels([monthly["mois"].astype(str).iloc[i] for i in ticks], rotation=45, ha="right")

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    plt.tight_layout()
    save_fig(fig, "temporal_monthly")


# ── 5. Top marchés par volume ─────────────────────────────────────────────────
def top_markets(con, parquet: str):
    logger.info("\n=== 5. Top 20 marchés par volume ===")

    top = con.execute(f"""
        SELECT
            question,
            ROUND(volume / 1e6, 2)  AS volume_M_usd,
            closed,
            outcome_prices
        FROM read_parquet('{parquet}')
        ORDER BY volume DESC
        LIMIT 20
    """).df()

    logger.info(top[["question", "volume_M_usd", "closed", "outcome_prices"]].to_string(index=False))
    top.to_csv(OUT_DIR / "top20_marches.csv", index=False)

    fig, ax = plt.subplots(figsize=(12, 8))
    labels = [q[:60] + "…" if len(q) > 60 else q for q in top["question"]]
    ax.barh(labels[::-1], top["volume_M_usd"][::-1], color="steelblue")
    ax.set_xlabel("Volume (M$)")
    ax.set_title("Top 20 marchés par volume")
    plt.tight_layout()
    save_fig(fig, "top20_marches")


# ── 6. Résolution YES vs NO ───────────────────────────────────────────────────
def resolution_analysis(con, parquet: str):
    logger.info("\n=== 6. Analyse des résolutions (outcome_prices) ===")

    # outcome_prices est une string type "['1', '0']" ou "['0', '1']"
    # '1' en première position = YES gagne, '0' = NO gagne
    res = con.execute(f"""
        SELECT
            CASE
                WHEN SUBSTRING(outcome_prices, 3, 1) = '1' THEN 'YES gagne'
                WHEN SUBSTRING(outcome_prices, 3, 1) = '0' THEN 'NO gagne'
                ELSE 'Indetermine'
            END                        AS resultat,
            COUNT(*)                   AS nb_marches,
            ROUND(SUM(volume)/1e6, 2)  AS volume_M_usd
        FROM read_parquet('{parquet}')
        WHERE closed = 1
        GROUP BY 1
        ORDER BY nb_marches DESC
    """).df()

    logger.info(res.to_string(index=False))
    res.to_csv(OUT_DIR / "resolution_yes_no.csv", index=False)

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.bar(res["resultat"], res["nb_marches"], color=["green", "red", "gray"])
    ax.set_ylabel("Nombre de marchés fermés")
    ax.set_title("Résolution YES vs NO (marchés fermés)")
    for i, (n, v) in enumerate(zip(res["nb_marches"], res["volume_M_usd"])):
        ax.text(i, n + 500, f"{n:,}\n({v}M$)", ha="center", fontsize=9)
    plt.tight_layout()
    save_fig(fig, "resolution_yes_no")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log_file = setup()
    parquet = str(DATA_DIR / "markets.parquet")

    logger.info("=" * 60)
    logger.info("PHASE 2 — EDA : markets.parquet")
    logger.info("=" * 60)

    con = duckdb.connect()

    overview(con, parquet)
    volume_distribution(con, parquet)
    market_categories(con, parquet)
    temporal_analysis(con, parquet)
    top_markets(con, parquet)
    resolution_analysis(con, parquet)

    con.close()

    logger.info(f"\nOutputs sauvegardes dans : {OUT_DIR.relative_to(PROJECT_ROOT)}")
    logger.info(f"Log : {log_file}")
    logger.success("EDA markets terminee.")


if __name__ == "__main__":
    main()
