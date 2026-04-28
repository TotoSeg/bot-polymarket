"""
Phase 3 — Edge #1 : Biais global NO
=====================================
Hypothèse : les marchés résolvent NO bien plus souvent que YES.
Si ce biais est SYSTÉMATIQUE par catégorie, on peut parier NO à l'aveugle
sur certains types de marchés et avoir un win rate positif.

Ce script mesure :
  1. Taux de résolution NO/YES par catégorie de marché
  2. Taux de résolution NO/YES par tranche de prix final
  3. EV (expected value) théorique d'un pari NO systématique
  4. Analyse du prix d'entrée optimal

Usage :
    python src/phase3_edges/edge_no_bias.py
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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
OUT_DIR      = PROJECT_ROOT / "outputs" / "phase3" / "no_bias"

MARKETS = str(DATA_DIR / "markets.parquet")
QUANT   = str(DATA_DIR / "quant.parquet")


def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_edge_no_bias.log"
    logger.remove()
    logger.add(sys.stdout, colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add(log_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
    return log_file


def save_fig(fig, name):
    p = OUT_DIR / f"{name}.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.success(f"Graphique : {p.relative_to(PROJECT_ROOT)}")


# ── 1. Taux NO/YES global et par catégorie ────────────────────────────────────
def no_rate_by_category(con):
    logger.info("=== 1. Taux de résolution NO par catégorie ===")

    df = con.execute(f"""
        SELECT
            CASE
                WHEN LOWER(question) LIKE '%bitcoin%' OR LOWER(question) LIKE '% btc%'
                     OR LOWER(question) LIKE '%ethereum%' OR LOWER(question) LIKE '% eth%'
                     OR LOWER(question) LIKE '%crypto%' OR LOWER(question) LIKE '%solana%'
                     OR LOWER(question) LIKE '%xrp%' OR LOWER(question) LIKE '%up or down%'
                     THEN 'Crypto'
                WHEN LOWER(question) LIKE '%president%' OR LOWER(question) LIKE '%election%'
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
                     OR LOWER(question) LIKE '%recession%'
                     THEN 'Economie'
                ELSE 'Autre'
            END                                                   AS categorie,
            COUNT(*)                                              AS nb_marches,
            -- Résolution : answer1='Yes' + outcome_prices commence par ['1' => YES gagne
            SUM(CASE WHEN SUBSTRING(outcome_prices,3,1)='1' THEN 1 ELSE 0 END) AS yes_gagne,
            SUM(CASE WHEN SUBSTRING(outcome_prices,3,1)='0' THEN 1 ELSE 0 END) AS no_gagne,
            ROUND(100.0 * SUM(CASE WHEN SUBSTRING(outcome_prices,3,1)='0' THEN 1 ELSE 0 END)
                  / COUNT(*), 1)                                  AS pct_no_gagne,
            ROUND(SUM(volume)/1e6, 1)                             AS volume_M_usd
        FROM read_parquet('{MARKETS}')
        WHERE closed = 1
          AND outcome_prices != '[]'
          AND SUBSTRING(outcome_prices,3,1) IN ('0','1')
        GROUP BY 1
        ORDER BY pct_no_gagne DESC
    """).df()

    logger.info(df.to_string(index=False))
    df.to_csv(OUT_DIR / "no_rate_by_category.csv", index=False)

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.bar(df["categorie"], df["pct_no_gagne"], color=["red" if v > 60 else "steelblue" for v in df["pct_no_gagne"]])
    ax.axhline(50, color="black", linestyle="--", linewidth=1, label="Aléatoire (50%)")
    ax.set_ylabel("% marchés résolus NO")
    ax.set_title("Taux de résolution NO par catégorie (marchés fermés)")
    ax.set_ylim(0, 100)
    for bar, val in zip(bars, df["pct_no_gagne"]):
        ax.text(bar.get_x() + bar.get_width()/2, val + 1, f"{val}%", ha="center", fontweight="bold")
    ax.legend()
    plt.tight_layout()
    save_fig(fig, "no_rate_by_category")
    return df


# ── 2. EV d'un pari NO systématique selon le prix d'entrée ───────────────────
def ev_by_entry_price(con):
    """
    Si on parie NO à un prix p_no (= 1 - p_yes) :
    - Si NO gagne (prob réelle r_no) : gain = (1 - p_no) / p_no = p_yes / p_no
    - Si YES gagne (prob réelle 1-r_no) : perte = 1 (on perd tout)

    EV = r_no * (p_yes/p_no) - (1-r_no) * 1
       = r_no * (p_yes/p_no) - (1 - r_no)

    On mesure r_no réel depuis les données historiques par tranche de prix final.
    """
    logger.info("\n=== 2. EV d'un pari NO par tranche de prix final ===")

    # Le prix final d'un marché = outcome_prices.
    # Pour mesurer l'edge, on regarde le dernier prix de trade AVANT résolution.
    # On approche par : pour chaque marché fermé, quel était le prix moyen des
    # trades dans les 24h avant clôture ?
    df = con.execute(f"""
        WITH marche_prix AS (
            -- Prix moyen des trades dans les dernières 24h de chaque marché
            SELECT
                m.id,
                m.closed,
                SUBSTRING(m.outcome_prices,3,1)                 AS resultat,
                AVG(q.price)                                     AS prix_final_moyen,
                COUNT(q.price)                                   AS nb_trades_fin
            FROM read_parquet('{MARKETS}') m
            JOIN read_parquet('{QUANT}') q ON q.market_id = m.id
            WHERE m.closed = 1
              AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
              AND epoch_ms(q.timestamp::BIGINT * 1000) >=
                  (m.end_date - INTERVAL '24 hours')
            GROUP BY m.id, m.closed, SUBSTRING(m.outcome_prices,3,1)
        )
        SELECT
            ROUND(FLOOR(prix_final_moyen / 0.05) * 0.05, 2)   AS bin_prix_yes,
            COUNT(*)                                             AS nb_marches,
            ROUND(100.0 * SUM(CASE WHEN resultat='0' THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_no_gagne,
            -- EV si on parie NO au prix implicite du bin (p_no = 1 - bin_prix_yes)
            ROUND(
                (SUM(CASE WHEN resultat='0' THEN 1 ELSE 0 END)::DOUBLE / COUNT(*))
                * (ROUND(FLOOR(prix_final_moyen / 0.05) * 0.05, 2)
                   / (1 - ROUND(FLOOR(prix_final_moyen / 0.05) * 0.05, 2)))
                - (SUM(CASE WHEN resultat='1' THEN 1 ELSE 0 END)::DOUBLE / COUNT(*))
            , 4) AS ev_pari_no
        FROM marche_prix
        WHERE prix_final_moyen BETWEEN 0.01 AND 0.99
          AND nb_trades_fin >= 3
        GROUP BY 1
        ORDER BY 1
    """).df()

    logger.info(df.to_string(index=False))
    df.to_csv(OUT_DIR / "ev_by_price.csv", index=False)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle("Edge du pari NO selon le prix d'entrée (YES)", fontsize=13)

    axes[0].bar(df["bin_prix_yes"].astype(str), df["pct_no_gagne"], color="coral")
    axes[0].axhline(50, color="black", linestyle="--", linewidth=1)
    axes[0].set_xlabel("Prix YES au moment du pari")
    axes[0].set_ylabel("% NO gagne réellement")
    axes[0].set_title("Taux de résolution NO réel")
    axes[0].tick_params(axis="x", rotation=90)

    colors = ["green" if v > 0 else "red" for v in df["ev_pari_no"]]
    axes[1].bar(df["bin_prix_yes"].astype(str), df["ev_pari_no"], color=colors)
    axes[1].axhline(0, color="black", linewidth=1)
    axes[1].set_xlabel("Prix YES au moment du pari")
    axes[1].set_ylabel("EV par unité misée")
    axes[1].set_title("Expected Value du pari NO")
    axes[1].tick_params(axis="x", rotation=90)

    plt.tight_layout()
    save_fig(fig, "ev_by_entry_price")
    return df


# ── 3. Drift du prix vers la résolution ───────────────────────────────────────
def price_drift_to_resolution(con):
    """
    Observe si le prix dérive systématiquement vers 0 (NO) ou 1 (YES)
    dans les jours précédant la résolution.
    """
    logger.info("\n=== 3. Drift du prix vers la résolution ===")

    df = con.execute(f"""
        WITH daily AS (
            SELECT
                q.market_id,
                SUBSTRING(m.outcome_prices,3,1)                             AS resultat,
                DATE_DIFF('day',
                    epoch_ms(q.timestamp::BIGINT*1000)::DATE,
                    m.end_date::DATE)                                        AS jours_avant_fin,
                AVG(q.price)                                                 AS prix_moyen
            FROM read_parquet('{QUANT}') q
            JOIN read_parquet('{MARKETS}') m ON q.market_id = m.id
            WHERE m.closed = 1
              AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
              AND DATE_DIFF('day',
                    epoch_ms(q.timestamp::BIGINT*1000)::DATE,
                    m.end_date::DATE) BETWEEN 0 AND 30
            GROUP BY q.market_id, SUBSTRING(m.outcome_prices,3,1),
                     DATE_DIFF('day', epoch_ms(q.timestamp::BIGINT*1000)::DATE, m.end_date::DATE)
        )
        SELECT
            jours_avant_fin,
            resultat,
            ROUND(AVG(prix_moyen), 4)   AS prix_yes_moyen,
            COUNT(DISTINCT market_id)   AS nb_marches
        FROM daily
        GROUP BY 1, 2
        ORDER BY 1, 2
    """).df()

    df.to_csv(OUT_DIR / "price_drift.csv", index=False)

    fig, ax = plt.subplots(figsize=(12, 5))
    for res, color, label in [("1", "green", "YES gagne"), ("0", "red", "NO gagne")]:
        sub = df[df["resultat"] == res]
        ax.plot(sub["jours_avant_fin"], sub["prix_yes_moyen"], color=color, label=label, linewidth=2)

    ax.axhline(0.5, color="gray", linestyle="--", linewidth=1)
    ax.invert_xaxis()
    ax.set_xlabel("Jours avant la résolution")
    ax.set_ylabel("Prix YES moyen")
    ax.set_title("Drift du prix YES vers la résolution (0 = jour de clôture)")
    ax.legend()
    plt.tight_layout()
    save_fig(fig, "price_drift")
    return df


def main():
    log_file = setup()
    logger.info("=" * 60)
    logger.info("PHASE 3 — Edge #1 : Biais Global NO")
    logger.info("=" * 60)

    con = duckdb.connect()
    con.execute("SET threads = 4; SET memory_limit = '4GB'")

    no_rate_by_category(con)
    ev_by_entry_price(con)
    price_drift_to_resolution(con)

    con.close()
    logger.success("Analyse biais NO terminee.")


if __name__ == "__main__":
    main()
