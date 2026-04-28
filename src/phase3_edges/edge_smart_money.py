"""
Phase 3 — Edge #3 : Smart Money / Whale Tracking
=================================================
Hypothèse : certains wallets ont un win rate significativement supérieur
à 50%. Si on les identifie et qu'on copie leurs positions, on peut
reproduire leur edge.

Ce script :
  1. Calcule le P&L historique de chaque wallet (maker)
  2. Identifie les "smart wallets" (win rate > 60%, volume > 10K$)
  3. Analyse leurs patterns d'entrée (à quel prix ils achètent/vendent)
  4. Vérifie si leur signal est prédictif AVANT la résolution
  5. Analyse les marchés que ces wallets choisissent

Usage :
    python src/phase3_edges/edge_smart_money.py
"""

import sys
from pathlib import Path
from datetime import datetime

import duckdb
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from loguru import logger

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
OUT_DIR      = PROJECT_ROOT / "outputs" / "phase3" / "smart_money"

MARKETS = str(DATA_DIR / "markets.parquet")
QUANT   = str(DATA_DIR / "quant.parquet")

# Seuils de qualification "smart wallet"
MIN_MARCHES     = 20    # Au moins 20 marchés différents pour statistiques fiables
MIN_VOLUME_USD  = 10000 # Au moins 10K$ de volume total


def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_edge_smart_money.log"
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


# ── 1. P&L par wallet ─────────────────────────────────────────────────────────
def wallet_pnl(con):
    """
    Pour chaque wallet, on calcule :
    - Position moyenne pondérée sur chaque marché
    - Résolution du marché
    - Profit/perte théorique

    Simplification : on estime le P&L par marché comme :
      Si le wallet a buyé YES en moyenne à p_entry, et YES gagne → profit = (1-p)/p
      Si YES perd → perte = 1 (perd sa mise)
    """
    logger.info("=== 1. P&L par wallet (top 200 wallets par volume) ===")
    logger.info("Attention : calcul sur les makers uniquement")

    df = con.execute(f"""
        WITH wallet_marche AS (
            -- Position nette de chaque wallet sur chaque marché
            SELECT
                q.maker                                                  AS wallet,
                q.market_id,
                AVG(CASE WHEN q.side='BUY'  THEN q.price END)           AS prix_achat_moyen,
                AVG(CASE WHEN q.side='SELL' THEN q.price END)           AS prix_vente_moyen,
                SUM(CASE WHEN q.side='BUY'  THEN q.usd_amount ELSE 0 END) AS montant_achete,
                SUM(CASE WHEN q.side='SELL' THEN q.usd_amount ELSE 0 END) AS montant_vendu,
                SUM(q.usd_amount)                                        AS volume_total,
                SUBSTRING(m.outcome_prices,3,1)                         AS resultat
            FROM read_parquet('{QUANT}') q
            JOIN read_parquet('{MARKETS}') m ON m.id = q.market_id
            WHERE m.closed = 1
              AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
            GROUP BY q.maker, q.market_id, SUBSTRING(m.outcome_prices,3,1)
        ),
        wallet_stats AS (
            SELECT
                wallet,
                COUNT(DISTINCT market_id)                                AS nb_marches,
                SUM(volume_total)                                        AS volume_usd,
                -- Win = avoir acheté YES et YES gagne OU vendu YES et NO gagne
                SUM(CASE
                    WHEN montant_achete > montant_vendu AND resultat='1' THEN 1
                    WHEN montant_vendu  > montant_achete AND resultat='0' THEN 1
                    ELSE 0
                END)                                                     AS nb_wins,
                SUM(CASE
                    WHEN montant_achete > montant_vendu AND resultat='0' THEN 1
                    WHEN montant_vendu  > montant_achete AND resultat='1' THEN 1
                    ELSE 0
                END)                                                     AS nb_losses
            FROM wallet_marche
            WHERE volume_total > 0
            GROUP BY wallet
        )
        SELECT
            wallet,
            nb_marches,
            ROUND(volume_usd, 0)                                        AS volume_usd,
            nb_wins,
            nb_losses,
            ROUND(100.0 * nb_wins / NULLIF(nb_wins + nb_losses, 0), 1)  AS win_rate_pct,
            ROUND(volume_usd / nb_marches, 0)                           AS volume_par_marche
        FROM wallet_stats
        WHERE nb_marches >= {MIN_MARCHES}
          AND volume_usd  >= {MIN_VOLUME_USD}
        ORDER BY win_rate_pct DESC
        LIMIT 200
    """).df()

    logger.info(f"Wallets qualifies (>= {MIN_MARCHES} marches, >= {MIN_VOLUME_USD}$): {len(df)}")
    logger.info("\nTop 20 wallets par win rate :")
    logger.info(df.head(20).to_string(index=False))
    df.to_csv(OUT_DIR / "wallet_pnl.csv", index=False)

    # Distribution des win rates
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.hist(df["win_rate_pct"].dropna(), bins=30, color="steelblue", edgecolor="white")
    ax.axvline(50, color="red", linestyle="--", label="Aléatoire (50%)")
    ax.axvline(df["win_rate_pct"].mean(), color="orange", linestyle="-",
               label=f"Moyenne ({df['win_rate_pct'].mean():.1f}%)")
    ax.set_xlabel("Win rate (%)")
    ax.set_ylabel("Nombre de wallets")
    ax.set_title(f"Distribution des win rates (wallets >= {MIN_MARCHES} marchés, >= {MIN_VOLUME_USD}$)")
    ax.legend()
    plt.tight_layout()
    save_fig(fig, "win_rate_distribution")
    return df


# ── 2. Smart wallets : analyse des patterns d'entrée ─────────────────────────
def smart_wallet_patterns(con, smart_wallets: list[str]):
    logger.info(f"\n=== 2. Patterns d'entree des {len(smart_wallets)} top wallets ===")

    wallets_str = "', '".join(smart_wallets[:50])  # Limiter à 50

    df = con.execute(f"""
        SELECT
            q.side,
            ROUND(FLOOR(q.price / 0.05) * 0.05, 2)     AS bin_prix,
            COUNT(*)                                      AS nb_trades,
            ROUND(SUM(q.usd_amount)/1e3, 1)             AS volume_K_usd,
            ROUND(AVG(q.usd_amount), 0)                  AS montant_moyen
        FROM read_parquet('{QUANT}') q
        WHERE q.maker IN ('{wallets_str}')
        GROUP BY q.side, ROUND(FLOOR(q.price / 0.05) * 0.05, 2)
        ORDER BY q.side, bin_prix
    """).df()

    df.to_csv(OUT_DIR / "smart_wallet_patterns.csv", index=False)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle("Patterns d'entrée des smart wallets (top win rate)", fontsize=12)

    for i, (side, color) in enumerate([("BUY", "green"), ("SELL", "red")]):
        sub = df[df["side"] == side]
        axes[i].bar(sub["bin_prix"].astype(str), sub["volume_K_usd"], color=color, alpha=0.8)
        axes[i].set_title(f"Trades {side} par niveau de prix")
        axes[i].set_xlabel("Prix YES")
        axes[i].set_ylabel("Volume (K$)")
        axes[i].tick_params(axis="x", rotation=90)

    plt.tight_layout()
    save_fig(fig, "smart_wallet_entry_patterns")
    return df


# ── 3. Timing : à quel moment les smart wallets entrent ──────────────────────
def smart_wallet_timing(con, smart_wallets: list[str]):
    logger.info("\n=== 3. Timing des smart wallets vs resolution ===")

    wallets_str = "', '".join(smart_wallets[:50])

    df = con.execute(f"""
        SELECT
            DATE_DIFF('day',
                epoch_ms(q.timestamp::BIGINT*1000)::DATE,
                m.end_date::DATE)                       AS jours_avant_fin,
            q.side,
            ROUND(AVG(q.price), 4)                      AS prix_moyen,
            COUNT(*)                                     AS nb_trades,
            ROUND(SUM(q.usd_amount)/1e3, 1)             AS volume_K_usd
        FROM read_parquet('{QUANT}') q
        JOIN read_parquet('{MARKETS}') m ON m.id = q.market_id
        WHERE q.maker IN ('{wallets_str}')
          AND m.closed = 1
          AND DATE_DIFF('day',
                epoch_ms(q.timestamp::BIGINT*1000)::DATE,
                m.end_date::DATE) BETWEEN 0 AND 30
        GROUP BY 1, 2
        ORDER BY 1, 2
    """).df()

    df.to_csv(OUT_DIR / "smart_wallet_timing.csv", index=False)

    fig, ax = plt.subplots(figsize=(12, 5))
    for side, color in [("BUY", "green"), ("SELL", "red")]:
        sub = df[df["side"] == side]
        ax.plot(sub["jours_avant_fin"], sub["volume_K_usd"], color=color, label=side, linewidth=2)

    ax.invert_xaxis()
    ax.set_xlabel("Jours avant résolution")
    ax.set_ylabel("Volume K$")
    ax.set_title("Quand les smart wallets entrent-ils en position ?")
    ax.legend()
    plt.tight_layout()
    save_fig(fig, "smart_wallet_timing")
    return df


def main():
    log_file = setup()
    logger.info("=" * 60)
    logger.info("PHASE 3 — Edge #3 : Smart Money / Whale Tracking")
    logger.info("=" * 60)

    con = duckdb.connect()
    con.execute("SET threads = 4; SET memory_limit = '4GB'")

    pnl_df = wallet_pnl(con)

    # Identifier les smart wallets (top 10% par win rate)
    threshold = pnl_df["win_rate_pct"].quantile(0.90)
    smart_wallets = pnl_df[pnl_df["win_rate_pct"] >= threshold]["wallet"].tolist()
    logger.info(f"\nSeuil smart wallet : win rate >= {threshold:.1f}%")
    logger.info(f"Nombre de smart wallets : {len(smart_wallets)}")

    if smart_wallets:
        smart_wallet_patterns(con, smart_wallets)
        smart_wallet_timing(con, smart_wallets)

    con.close()
    logger.success("Analyse Smart Money terminee.")


if __name__ == "__main__":
    main()
