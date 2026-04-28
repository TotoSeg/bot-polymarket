"""
Phase 3 — Edge #2 : "Nothing Ever Happens" + Événements impossibles
=====================================================================

STRATÉGIE A — "Nothing Ever Happens"
  Parier NO quand le marché pense que YES a déjà < 5% ou < 10% de chances.
  Ces marchés sont déjà quasi-certains de résoudre NO.
  On gratte les quelques % restants avec un win rate très élevé.

  Logique financière :
  - Prix YES = 0.05 → Prix NO = 0.95
  - Si on achète NO et que NO gagne : profit = 0.05/0.95 = +5.26%
  - Si on se trompe et YES gagne : perte = 95% de la mise
  - Edge si : taux réel de résolution NO > prix implicite du marché (0.95)

STRATÉGIE B — Événements physiquement impossibles
  Certains marchés posent des questions absurdes.
  Le prix YES devrait être 0, mais le marché le fixe à 1-5%.
  → Profit garanti si on identifie correctement les impossibilités.

Ce script :
  1. Identifie les marchés "rien ne se passe" (prix YES < 5%, 10%, 15%)
  2. Mesure leur taux de résolution réel (validation historique)
  3. Calcule l'EV et le win rate de la stratégie
  4. Détecte les patterns de mots-clés pour les événements impossibles
  5. Estime les opportunités futures (combien par mois)

Usage :
    python src/phase3_edges/edge_nothing_happens.py
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
OUT_DIR      = PROJECT_ROOT / "outputs" / "phase3" / "nothing_happens"

MARKETS = str(DATA_DIR / "markets.parquet")
QUANT   = str(DATA_DIR / "quant.parquet")

# Mots-clés d'événements considérés comme "physiquement impossibles" ou
# clairement improbables selon bon sens
IMPOSSIBLE_KEYWORDS = [
    "jesus", "christ", "god", "alien", "zombie", "apocalypse",
    "world war iii", "world war 3", "wwiii", "ww3",
    "nuclear war", "nuclear attack", "end of the world",
    "asteroid", "meteor", "rapture", "second coming",
    "flat earth", "moon landing fake", "illuminati",
    "time travel", "teleportation",
]

# Mots-clés "rien ne se passe" — événements très peu probables par nature
NOTHING_HAPPENS_KEYWORDS = [
    "never", "impossible", "will not", "won't", "no chance",
    "declare war", "invade", "coup", "assassination", "impeach",
    "martial law", "default", "collapse", "crash", "ban",
]


def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_edge_nothing_happens.log"
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


# ── A. Stratégie "Nothing Ever Happens" ───────────────────────────────────────
def nothing_ever_happens(con):
    """
    On prend le DERNIER prix de trade de chaque marché comme proxy du
    'prix d'entrée'. Si ce dernier prix YES < seuil, on simule un pari NO.
    """
    logger.info("=== STRATEGIE A : Nothing Ever Happens ===")
    logger.info("Marchés dont le dernier prix YES < 5%, 10%, 15%")

    # Dernier prix connu avant résolution pour chaque marché
    df = con.execute(f"""
        WITH last_trade AS (
            SELECT
                market_id,
                LAST(price ORDER BY timestamp)  AS dernier_prix_yes,
                COUNT(*)                         AS nb_trades
            FROM read_parquet('{QUANT}')
            GROUP BY market_id
        ),
        marches_fermes AS (
            SELECT
                m.id,
                m.question,
                SUBSTRING(m.outcome_prices,3,1)          AS resultat,
                m.volume,
                lt.dernier_prix_yes,
                lt.nb_trades
            FROM read_parquet('{MARKETS}') m
            JOIN last_trade lt ON lt.market_id = m.id
            WHERE m.closed = 1
              AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
              AND lt.dernier_prix_yes > 0
              AND lt.nb_trades >= 5         -- Au moins 5 trades pour avoir un prix fiable
        )
        SELECT
            CASE
                WHEN dernier_prix_yes <= 0.05 THEN 'A. YES <= 5%'
                WHEN dernier_prix_yes <= 0.10 THEN 'B. YES <= 10%'
                WHEN dernier_prix_yes <= 0.15 THEN 'C. YES <= 15%'
                WHEN dernier_prix_yes <= 0.20 THEN 'D. YES <= 20%'
                ELSE                               'E. YES > 20%'
            END                                                         AS seuil,
            COUNT(*)                                                     AS nb_marches,
            SUM(CASE WHEN resultat='0' THEN 1 ELSE 0 END)               AS no_gagne,
            ROUND(100.0 * SUM(CASE WHEN resultat='0' THEN 1 ELSE 0 END) / COUNT(*), 2) AS win_rate_pct,
            ROUND(AVG(dernier_prix_yes), 4)                             AS prix_yes_moyen,
            -- EV = win_rate * (prix_yes / prix_no) - (1-win_rate)
            ROUND(
                (SUM(CASE WHEN resultat='0' THEN 1 ELSE 0 END)::DOUBLE / COUNT(*))
                * (AVG(dernier_prix_yes) / (1 - AVG(dernier_prix_yes)))
                - (SUM(CASE WHEN resultat='1' THEN 1 ELSE 0 END)::DOUBLE / COUNT(*))
            , 4)                                                         AS ev_par_unite,
            ROUND(AVG(volume), 0)                                        AS volume_moyen_usd
        FROM marches_fermes
        GROUP BY 1
        ORDER BY 1
    """).df()

    logger.info(df.to_string(index=False))
    df.to_csv(OUT_DIR / "nothing_happens_by_threshold.csv", index=False)

    # Graphique win rate + EV
    sub = df[df["seuil"] != "E. YES > 20%"]
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig.suptitle("Stratégie 'Nothing Ever Happens' — Parier NO quand YES est déjà < seuil", fontsize=12)

    colors_wr = ["green" if v >= 90 else "orange" if v >= 75 else "red" for v in sub["win_rate_pct"]]
    axes[0].bar(sub["seuil"], sub["win_rate_pct"], color=colors_wr)
    axes[0].axhline(95, color="green", linestyle="--", label="95%")
    axes[0].axhline(90, color="orange", linestyle="--", label="90%")
    axes[0].set_ylabel("Win rate (%)")
    axes[0].set_title("Taux de victoire historique")
    axes[0].set_ylim(0, 100)
    axes[0].legend()
    for i, (s, v, n) in enumerate(zip(sub["seuil"], sub["win_rate_pct"], sub["nb_marches"])):
        axes[0].text(i, v + 0.5, f"{v}%\n(n={n:,})", ha="center", fontsize=8)

    colors_ev = ["green" if v > 0 else "red" for v in sub["ev_par_unite"]]
    axes[1].bar(sub["seuil"], sub["ev_par_unite"] * 100, color=colors_ev)
    axes[1].axhline(0, color="black", linewidth=1)
    axes[1].set_ylabel("EV (%)")
    axes[1].set_title("Expected Value par trade")
    for i, (s, v) in enumerate(zip(sub["seuil"], sub["ev_par_unite"])):
        axes[1].text(i, v * 100 + 0.1, f"{v*100:.1f}%", ha="center", fontsize=9)

    plt.tight_layout()
    save_fig(fig, "nothing_happens_results")
    return df


# ── B. Exemples de marchés "Nothing Ever Happens" actifs ──────────────────────
def sample_opportunities(con):
    logger.info("\n=== Exemples de marchés 'Nothing Ever Happens' (meilleurs cas) ===")

    df = con.execute(f"""
        WITH last_trade AS (
            SELECT market_id, LAST(price ORDER BY timestamp) AS dernier_prix_yes
            FROM read_parquet('{QUANT}')
            GROUP BY market_id
        )
        SELECT
            m.question,
            ROUND(lt.dernier_prix_yes, 3)                   AS prix_yes,
            ROUND(1 - lt.dernier_prix_yes, 3)               AS prix_no,
            ROUND(lt.dernier_prix_yes / (1-lt.dernier_prix_yes) * 100, 1) AS gain_si_no_pct,
            ROUND(m.volume, 0)                               AS volume_usd,
            m.closed,
            SUBSTRING(m.outcome_prices,3,1)                 AS resultat
        FROM read_parquet('{MARKETS}') m
        JOIN last_trade lt ON lt.market_id = m.id
        WHERE lt.dernier_prix_yes <= 0.10
          AND m.volume > 10000
          AND m.closed = 1
          AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
          -- Exclure crypto Up/Down (trop aléatoire)
          AND LOWER(m.question) NOT LIKE '%up or down%'
          AND LOWER(m.question) NOT LIKE '%updown%'
        ORDER BY volume_usd DESC
        LIMIT 30
    """).df()

    logger.info(df.to_string(index=False))
    df.to_csv(OUT_DIR / "sample_opportunities.csv", index=False)
    return df


# ── C. Événements impossibles / absurdes ──────────────────────────────────────
def impossible_events(con):
    logger.info("\n=== STRATEGIE B : Evenements impossibles/absurdes ===")

    kw_filter = " OR ".join([f"LOWER(question) LIKE '%{kw}%'" for kw in IMPOSSIBLE_KEYWORDS])

    df = con.execute(f"""
        WITH last_trade AS (
            SELECT market_id, LAST(price ORDER BY timestamp) AS dernier_prix_yes
            FROM read_parquet('{QUANT}')
            GROUP BY market_id
        )
        SELECT
            m.question,
            ROUND(lt.dernier_prix_yes, 3)           AS prix_yes,
            ROUND(m.volume, 0)                       AS volume_usd,
            SUBSTRING(m.outcome_prices,3,1)         AS resultat,
            m.closed
        FROM read_parquet('{MARKETS}') m
        LEFT JOIN last_trade lt ON lt.market_id = m.id
        WHERE ({kw_filter})
        ORDER BY volume_usd DESC
        LIMIT 50
    """).df()

    if len(df) > 0:
        logger.info(f"Trouves {len(df)} marches avec mots-cles 'impossibles'")
        logger.info(df.to_string(index=False))
        df.to_csv(OUT_DIR / "impossible_events.csv", index=False)
    else:
        logger.warning("Aucun marche 'impossible' trouve avec les mots-cles actuels")

    return df


# ── D. Fréquence des opportunités ─────────────────────────────────────────────
def opportunity_frequency(con):
    logger.info("\n=== Frequence des opportunites (nouveaux marches YES < 10%) ===")

    df = con.execute(f"""
        WITH last_trade AS (
            SELECT market_id, LAST(price ORDER BY timestamp) AS dernier_prix_yes
            FROM read_parquet('{QUANT}')
            GROUP BY market_id
        )
        SELECT
            DATE_TRUNC('month', m.created_at)::DATE     AS mois,
            COUNT(*)                                      AS nb_opportunites,
            ROUND(SUM(m.volume)/1e3, 1)                  AS volume_total_K_usd
        FROM read_parquet('{MARKETS}') m
        JOIN last_trade lt ON lt.market_id = m.id
        WHERE lt.dernier_prix_yes <= 0.10
          AND m.volume > 1000
          AND LOWER(m.question) NOT LIKE '%up or down%'
        GROUP BY 1
        ORDER BY 1
    """).df()

    logger.info(f"Moyenne mensuelle : {df['nb_opportunites'].mean():.0f} opportunites/mois")
    logger.info(f"Volume moyen disponible : {df['volume_total_K_usd'].mean():.0f}K$/mois")
    df.to_csv(OUT_DIR / "opportunity_frequency.csv", index=False)

    fig, ax = plt.subplots(figsize=(13, 4))
    ax.bar(df["mois"].astype(str), df["nb_opportunites"], color="steelblue")
    ax.set_title("Nombre d'opportunites 'Nothing Ever Happens' par mois (YES < 10%, vol > 1K$)")
    ax.set_ylabel("Nb marches")
    ticks = list(range(0, len(df), max(1, len(df)//12)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([df["mois"].astype(str).iloc[i] for i in ticks], rotation=45, ha="right")
    plt.tight_layout()
    save_fig(fig, "opportunity_frequency")
    return df


def main():
    log_file = setup()
    logger.info("=" * 60)
    logger.info("PHASE 3 — Edge #2 : Nothing Ever Happens + Impossible")
    logger.info("=" * 60)

    con = duckdb.connect()
    con.execute("SET threads = 4; SET memory_limit = '4GB'")

    nothing_ever_happens(con)
    sample_opportunities(con)
    impossible_events(con)
    opportunity_frequency(con)

    con.close()
    logger.success("Analyse Nothing Ever Happens terminee.")


if __name__ == "__main__":
    main()
