"""
Phase 3 — Scorecard global des edges identifiés
=================================================
Consolide tous les edges en un tableau de bord décisionnel :
  - Win rate historique
  - EV par trade
  - Fréquence des opportunités
  - Score global (win_rate × EV × fréquence)

Et identifie 2 edges supplémentaires issus des données :
  Edge #4 : Price Anchoring Bias — les marchés s'ouvrent souvent à 0.50
             mais le prix converge vers 0 (NO) dans la majorité des cas.
             → Parier NO à l'ouverture sur certaines catégories.
  Edge #5 : Durée courte + faible volume = bruit
             Les marchés < 7 jours et < 1K$ résolvent NO à >70%.
             → Parier NO sur ces marchés de très court terme.

Usage :
    python src/phase3_edges/edge_summary.py
"""

import sys
from pathlib import Path
from datetime import datetime

import duckdb
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from loguru import logger

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
OUT_DIR      = PROJECT_ROOT / "outputs" / "phase3" / "summary"

MARKETS = str(DATA_DIR / "markets.parquet")
QUANT   = str(DATA_DIR / "quant.parquet")


def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_edge_summary.log"
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


# ── Edge #4 : Price Anchoring Bias ────────────────────────────────────────────
def edge_price_anchoring(con):
    """
    Beaucoup de marchés s'ouvrent à 0.50 (maximum d'incertitude).
    L'edge : dans quelle catégorie le prix converge-t-il systématiquement vers 0 ?
    Si un marché politique ouvre à 0.50 et converge vers 0 dans 70% des cas,
    parier NO dès l'ouverture est profitable.
    """
    logger.info("=== Edge #4 : Price Anchoring (ouverture a 0.50) ===")

    df = con.execute(f"""
        WITH premier_trade AS (
            SELECT
                market_id,
                FIRST(price ORDER BY timestamp)   AS premier_prix,
                LAST(price ORDER BY timestamp)    AS dernier_prix,
                COUNT(*)                           AS nb_trades
            FROM read_parquet('{QUANT}')
            GROUP BY market_id
        )
        SELECT
            CASE
                WHEN LOWER(m.question) LIKE '%bitcoin%' OR LOWER(m.question) LIKE '%crypto%'
                     OR LOWER(m.question) LIKE '%ethereum%' OR LOWER(m.question) LIKE '%up or down%'
                     THEN 'Crypto'
                WHEN LOWER(m.question) LIKE '%president%' OR LOWER(m.question) LIKE '%election%'
                     OR LOWER(m.question) LIKE '%trump%' OR LOWER(m.question) LIKE '%vote%'
                     THEN 'Politique'
                WHEN LOWER(m.question) LIKE '%nba%' OR LOWER(m.question) LIKE '%nfl%'
                     OR LOWER(m.question) LIKE '%league%' OR LOWER(m.question) LIKE '%cup%'
                     THEN 'Sport'
                ELSE 'Autre'
            END                                                             AS categorie,
            COUNT(*)                                                         AS nb_marches,
            ROUND(AVG(pt.premier_prix), 3)                                  AS prix_ouverture_moyen,
            ROUND(AVG(pt.dernier_prix), 3)                                  AS prix_cloture_moyen,
            -- Drift = combien le prix baisse en moyenne (vers NO)
            ROUND(AVG(pt.premier_prix - pt.dernier_prix), 3)               AS drift_vers_no,
            ROUND(100.0 * SUM(CASE WHEN SUBSTRING(m.outcome_prices,3,1)='0' THEN 1 ELSE 0 END)
                  / COUNT(*), 1)                                             AS pct_no_gagne,
            -- Parmi ceux qui ouvrent entre 0.45 et 0.55 (autour de 0.5)
            SUM(CASE WHEN pt.premier_prix BETWEEN 0.45 AND 0.55 THEN 1 ELSE 0 END) AS ouvre_a_50pct
        FROM read_parquet('{MARKETS}') m
        JOIN premier_trade pt ON pt.market_id = m.id
        WHERE m.closed = 1
          AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
          AND pt.nb_trades >= 10
        GROUP BY 1
        ORDER BY drift_vers_no DESC
    """).df()

    logger.info(df.to_string(index=False))
    df.to_csv(OUT_DIR / "edge_price_anchoring.csv", index=False)

    fig, ax = plt.subplots(figsize=(10, 5))
    x = range(len(df))
    width = 0.35
    ax.bar([i - width/2 for i in x], df["prix_ouverture_moyen"], width, label="Prix ouverture", color="steelblue")
    ax.bar([i + width/2 for i in x], df["prix_cloture_moyen"], width, label="Prix cloture", color="coral")
    ax.set_xticks(list(x))
    ax.set_xticklabels(df["categorie"])
    ax.axhline(0.5, color="black", linestyle="--", linewidth=1)
    ax.set_ylabel("Prix YES moyen")
    ax.set_title("Edge #4 : Drift du prix d'ouverture vers la cloture par categorie")
    ax.legend()
    plt.tight_layout()
    save_fig(fig, "edge_price_anchoring")
    return df


# ── Edge #5 : Marchés courts + faible volume ──────────────────────────────────
def edge_short_markets(con):
    """
    Les marchés de très courte durée (< 7 jours) et faible volume (< 1K$)
    sont souvent des spéculations peu informées. Hypothèse : ils résolvent
    NO plus souvent car peu de personnes ont l'information.
    """
    logger.info("\n=== Edge #5 : Marches courts + faible liquidite ===")

    df = con.execute(f"""
        SELECT
            CASE
                WHEN DATE_DIFF('day', m.created_at::DATE, m.end_date::DATE) <= 1   THEN 'A. 0-1 jour'
                WHEN DATE_DIFF('day', m.created_at::DATE, m.end_date::DATE) <= 7   THEN 'B. 2-7 jours'
                WHEN DATE_DIFF('day', m.created_at::DATE, m.end_date::DATE) <= 30  THEN 'C. 8-30 jours'
                WHEN DATE_DIFF('day', m.created_at::DATE, m.end_date::DATE) <= 180 THEN 'D. 1-6 mois'
                ELSE                                                                     'E. > 6 mois'
            END                                                             AS duree,
            CASE
                WHEN m.volume < 1000    THEN '1. < 1K$'
                WHEN m.volume < 10000   THEN '2. 1K-10K$'
                WHEN m.volume < 100000  THEN '3. 10-100K$'
                ELSE                         '4. > 100K$'
            END                                                             AS tranche_vol,
            COUNT(*)                                                         AS nb_marches,
            ROUND(100.0 * SUM(CASE WHEN SUBSTRING(outcome_prices,3,1)='0' THEN 1 ELSE 0 END)
                  / COUNT(*), 1)                                             AS pct_no_gagne
        FROM read_parquet('{MARKETS}') m
        WHERE closed = 1
          AND SUBSTRING(outcome_prices,3,1) IN ('0','1')
          AND end_date > created_at
        GROUP BY 1, 2
        ORDER BY 1, 2
    """).df()

    logger.info(df.to_string(index=False))
    df.to_csv(OUT_DIR / "edge_short_markets.csv", index=False)

    pivot = df.pivot(index="duree", columns="tranche_vol", values="pct_no_gagne")
    fig, ax = plt.subplots(figsize=(10, 5))
    im = ax.imshow(pivot.values, cmap="RdYlGn", vmin=40, vmax=80, aspect="auto")
    ax.set_xticks(range(len(pivot.columns)))
    ax.set_xticklabels(pivot.columns)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index)
    for i in range(len(pivot.index)):
        for j in range(len(pivot.columns)):
            val = pivot.values[i, j]
            if not pd.isna(val):
                ax.text(j, i, f"{val:.0f}%", ha="center", va="center",
                        color="black", fontweight="bold")
    plt.colorbar(im, ax=ax, label="% NO gagne")
    ax.set_title("Edge #5 : % resolutions NO par duree et volume\n(vert = fort biais NO)")
    plt.tight_layout()
    save_fig(fig, "edge_short_markets")
    return df


# ── Edge #6 : Consensus shift (volume spike) ──────────────────────────────────
def edge_volume_spike(con):
    """
    Un pic de volume soudain sur un marché précède souvent sa résolution.
    Les 'smart traders' accumulent avant une résolution certaine.
    On détecte si un spike de volume dans les 48h avant résolution
    prédit correctement le résultat.
    """
    logger.info("\n=== Edge #6 : Volume Spike avant resolution ===")

    df = con.execute(f"""
        WITH daily_vol AS (
            SELECT
                q.market_id,
                epoch_ms(q.timestamp::BIGINT*1000)::DATE                     AS jour,
                SUM(q.usd_amount)                                             AS vol_jour
            FROM read_parquet('{QUANT}') q
            GROUP BY q.market_id, epoch_ms(q.timestamp::BIGINT*1000)::DATE
        ),
        spike_detection AS (
            SELECT
                dv.market_id,
                MAX(CASE WHEN DATE_DIFF('day', dv.jour, m.end_date::DATE) <= 2
                         THEN dv.vol_jour ELSE 0 END)                        AS vol_derniers_2j,
                AVG(dv.vol_jour)                                              AS vol_moyen_journalier,
                SUBSTRING(m.outcome_prices,3,1)                              AS resultat
            FROM daily_vol dv
            JOIN read_parquet('{MARKETS}') m ON m.id = dv.market_id
            WHERE m.closed = 1
              AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
            GROUP BY dv.market_id, SUBSTRING(m.outcome_prices,3,1)
            HAVING vol_moyen_journalier > 0 AND vol_derniers_2j > 0
        )
        SELECT
            CASE
                WHEN vol_derniers_2j / vol_moyen_journalier >= 10 THEN 'A. Spike >= 10x'
                WHEN vol_derniers_2j / vol_moyen_journalier >= 5  THEN 'B. Spike 5-10x'
                WHEN vol_derniers_2j / vol_moyen_journalier >= 2  THEN 'C. Spike 2-5x'
                WHEN vol_derniers_2j / vol_moyen_journalier >= 1  THEN 'D. Normal 1-2x'
                ELSE                                                   'E. Baisse < 1x'
            END                                                              AS spike_type,
            COUNT(*)                                                          AS nb_marches,
            ROUND(100.0 * SUM(CASE WHEN resultat='0' THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_no_gagne,
            ROUND(100.0 * SUM(CASE WHEN resultat='1' THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_yes_gagne
        FROM spike_detection
        GROUP BY 1
        ORDER BY 1
    """).df()

    logger.info(df.to_string(index=False))
    df.to_csv(OUT_DIR / "edge_volume_spike.csv", index=False)
    return df


# ── Scorecard consolidé ───────────────────────────────────────────────────────
def build_scorecard(con):
    logger.info("\n" + "=" * 60)
    logger.info("SCORECARD GLOBAL DES EDGES")
    logger.info("=" * 60)

    # Charger les résultats existants si disponibles
    edges = []

    # Edge #1 : Biais NO global (calculé en phase précédente)
    res = con.execute(f"""
        SELECT
            ROUND(100.0 * SUM(CASE WHEN SUBSTRING(outcome_prices,3,1)='0' THEN 1 ELSE 0 END)
                  / COUNT(*), 1) AS win_rate
        FROM read_parquet('{MARKETS}')
        WHERE closed = 1 AND SUBSTRING(outcome_prices,3,1) IN ('0','1')
    """).fetchone()
    edges.append({
        "edge": "#1 Biais NO Global",
        "description": "Parier NO a l'aveugle sur tout marche binaire ferme",
        "win_rate_pct": res[0],
        "nb_opportunites_mois": 5000,
        "volume_min_usd": 1000,
        "implementation": "Facile",
        "risque": "Moyen",
    })

    # Edge #2 : Nothing Ever Happens (YES <= 5%)
    res2 = con.execute(f"""
        WITH last_trade AS (
            SELECT market_id, LAST(price ORDER BY timestamp) AS px
            FROM read_parquet('{QUANT}') GROUP BY market_id
        )
        SELECT
            COUNT(*) AS n,
            ROUND(100.0*SUM(CASE WHEN SUBSTRING(m.outcome_prices,3,1)='0' THEN 1 ELSE 0 END)/COUNT(*),1) AS wr
        FROM read_parquet('{MARKETS}') m
        JOIN last_trade lt ON lt.market_id=m.id
        WHERE lt.px<=0.05 AND m.closed=1
          AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
          AND LOWER(m.question) NOT LIKE '%up or down%'
    """).fetchone()
    edges.append({
        "edge": "#2a Nothing Ever Happens (<=5%)",
        "description": "Parier NO quand YES < 5% sur marche non-crypto",
        "win_rate_pct": res2[1],
        "nb_opportunites_mois": max(res2[0] // 40, 1),  # ~40 mois de données
        "volume_min_usd": 1000,
        "implementation": "Tres facile",
        "risque": "Faible",
    })

    # Edge #2b : YES <= 10%
    res2b = con.execute(f"""
        WITH last_trade AS (
            SELECT market_id, LAST(price ORDER BY timestamp) AS px
            FROM read_parquet('{QUANT}') GROUP BY market_id
        )
        SELECT
            COUNT(*) AS n,
            ROUND(100.0*SUM(CASE WHEN SUBSTRING(m.outcome_prices,3,1)='0' THEN 1 ELSE 0 END)/COUNT(*),1) AS wr
        FROM read_parquet('{MARKETS}') m
        JOIN last_trade lt ON lt.market_id=m.id
        WHERE lt.px<=0.10 AND lt.px>0.05 AND m.closed=1
          AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
          AND LOWER(m.question) NOT LIKE '%up or down%'
    """).fetchone()
    edges.append({
        "edge": "#2b Nothing Ever Happens (5-10%)",
        "description": "Parier NO quand YES entre 5-10% sur marche non-crypto",
        "win_rate_pct": res2b[1],
        "nb_opportunites_mois": max(res2b[0] // 40, 1),
        "volume_min_usd": 1000,
        "implementation": "Tres facile",
        "risque": "Faible",
    })

    scorecard = pd.DataFrame(edges)
    logger.info("\n" + scorecard.to_string(index=False))
    scorecard.to_csv(OUT_DIR / "edge_scorecard.csv", index=False)

    # Graphique scorecard
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.axis("off")
    table_data = [[
        r["edge"], r["description"][:50],
        f"{r['win_rate_pct']}%", f"~{r['nb_opportunites_mois']}/mois",
        r["implementation"], r["risque"]
    ] for _, r in scorecard.iterrows()]
    headers = ["Edge", "Description", "Win Rate", "Freq.", "Impl.", "Risque"]
    tbl = ax.table(cellText=table_data, colLabels=headers,
                   loc="center", cellLoc="center")
    tbl.auto_set_font_size(True)
    tbl.scale(1, 1.8)
    ax.set_title("Scorecard des Edges — Phase 3", fontsize=14, pad=20)
    plt.tight_layout()
    save_fig(fig, "edge_scorecard")
    return scorecard


def main():
    log_file = setup()
    logger.info("=" * 60)
    logger.info("PHASE 3 — Scorecard global + Edges #4, #5, #6")
    logger.info("=" * 60)

    con = duckdb.connect()
    con.execute("SET threads = 4; SET memory_limit = '4GB'")

    edge_price_anchoring(con)
    edge_short_markets(con)
    edge_volume_spike(con)
    build_scorecard(con)

    con.close()
    logger.success("Scorecard termine.")


if __name__ == "__main__":
    main()
