"""
Phase 4 — Backtest de toutes les stratégies
=============================================
7 stratégies testées sur données historiques (2022-2026) :

  S1 - Biais NO Global          : parier NO sur tout marché non-crypto
  S2 - Nothing Ever Happens 5%  : parier NO quand YES < 5%
  S3 - Nothing Ever Happens 10% : parier NO quand YES entre 5-10%
  S4 - Nothing Ever Happens 20% : parier NO quand YES entre 10-20%
  S5 - Événements impossibles   : parier NO sur Jesus/Aliens/WW3/etc.
  S6 - Long duration + low vol  : marchés > 1 mois, volume < 10K$
  S7 - Volume décroissant       : volume baisse dans les 48h avant résolution

Pour chaque stratégie :
  - Prix d'entrée = dernier prix disponible avant la résolution (sans look-ahead)
  - Win rate prior = taux historique observé en phase 3 (pour Kelly)
  - Simulation trade par trade avec Kelly 25%

Usage :
    python src/phase4_backtest/backtest_strategies.py
"""

import sys
from pathlib import Path
from datetime import datetime

import duckdb
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from loguru import logger

# Import du moteur de backtest (même package)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.phase4_backtest.backtest_engine import simulate_trades, print_metrics

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
OUT_DIR      = PROJECT_ROOT / "outputs" / "phase4"

MARKETS = str(DATA_DIR / "markets.parquet")
QUANT   = str(DATA_DIR / "quant.parquet")

# Capital de départ simulé
INITIAL_CAPITAL = 1000.0

# Mots-clés pour les événements impossibles/absurdes (S5)
IMPOSSIBLE_KEYWORDS = [
    "jesus", "second coming", "rapture",
    "alien", "aliens exist", "ufo confirmed", "extraterrestrial confirmed",
    "world war iii", "world war 3", "wwiii", "ww3", "nuclear war",
    "end of the world", "apocalypse", "asteroid hits",
    "zombie", "flat earth confirmed",
    "time travel", "teleportation confirmed",
]

# Mots-clés catégories pour S1 (exclure crypto)
CRYPTO_KEYWORDS = [
    "bitcoin", " btc", "ethereum", " eth", "solana", "xrp", "crypto",
    "up or down", "updown",
]


def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_backtest.log"
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


# ── Extraction des trades pour chaque stratégie ───────────────────────────────

def get_trades_s1_no_global(con) -> pd.DataFrame:
    """S1 : Parier NO sur tout marché non-crypto fermé."""
    logger.info("Extraction S1 : Biais NO Global (hors crypto)...")
    kw_filter = " AND ".join([f"LOWER(m.question) NOT LIKE '%{k}%'" for k in CRYPTO_KEYWORDS])
    df = con.execute(f"""
        WITH last_trade AS (
            SELECT market_id,
                   LAST(price ORDER BY timestamp)   AS entry_price_yes,
                   COUNT(*)                          AS nb_trades
            FROM read_parquet('{QUANT}')
            GROUP BY market_id
        )
        SELECT
            m.id         AS market_id,
            m.question,
            lt.entry_price_yes,
            CAST(SUBSTRING(m.outcome_prices,3,1) AS INTEGER) AS outcome,
            m.end_date   AS timestamp
        FROM read_parquet('{MARKETS}') m
        JOIN last_trade lt ON lt.market_id = m.id
        WHERE m.closed = 1
          AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
          AND lt.nb_trades >= 5
          AND lt.entry_price_yes BETWEEN 0.05 AND 0.95
          AND m.volume >= 1000
          AND {kw_filter}
        ORDER BY m.end_date
    """).df()
    df["win_rate_prior"] = 0.672  # Taux historique phase 3
    logger.info(f"  -> {len(df):,} trades")
    return df


def get_trades_s2_nothing_5pct(con) -> pd.DataFrame:
    """S2 : Parier NO quand le dernier prix YES < 5% avant résolution (hors crypto).
    On utilise le DERNIER prix (comme phase 3), pas le premier passage sous 5%,
    pour éviter de capturer des marchés qui ont rebondi ensuite vers YES.
    """
    logger.info("Extraction S2 : Nothing Ever Happens YES < 5%...")
    kw_filter = " AND ".join([f"LOWER(m.question) NOT LIKE '%{k}%'" for k in CRYPTO_KEYWORDS])
    df = con.execute(f"""
        WITH last_trade AS (
            SELECT market_id,
                   LAST(price ORDER BY timestamp) AS entry_price_yes,
                   COUNT(*)                        AS nb_trades
            FROM read_parquet('{QUANT}')
            GROUP BY market_id
        )
        SELECT
            m.id         AS market_id,
            m.question,
            lt.entry_price_yes,
            CAST(SUBSTRING(m.outcome_prices,3,1) AS INTEGER) AS outcome,
            m.end_date   AS timestamp
        FROM read_parquet('{MARKETS}') m
        JOIN last_trade lt ON lt.market_id = m.id
        WHERE m.closed = 1
          AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
          AND lt.nb_trades >= 5
          AND lt.entry_price_yes BETWEEN 0.001 AND 0.049
          AND m.volume >= 1000
          AND {kw_filter}
        ORDER BY m.end_date
    """).df()
    df["win_rate_prior"] = 0.999
    logger.info(f"  -> {len(df):,} trades")
    return df


def get_trades_s3_nothing_10pct(con) -> pd.DataFrame:
    """S3 : Parier NO quand le dernier prix YES est entre 5% et 10%."""
    logger.info("Extraction S3 : Nothing Ever Happens YES 5-10%...")
    kw_filter = " AND ".join([f"LOWER(m.question) NOT LIKE '%{k}%'" for k in CRYPTO_KEYWORDS])
    df = con.execute(f"""
        WITH last_trade AS (
            SELECT market_id,
                   LAST(price ORDER BY timestamp) AS entry_price_yes,
                   COUNT(*)                        AS nb_trades
            FROM read_parquet('{QUANT}')
            GROUP BY market_id
        )
        SELECT
            m.id         AS market_id,
            m.question,
            lt.entry_price_yes,
            CAST(SUBSTRING(m.outcome_prices,3,1) AS INTEGER) AS outcome,
            m.end_date   AS timestamp
        FROM read_parquet('{MARKETS}') m
        JOIN last_trade lt ON lt.market_id = m.id
        WHERE m.closed = 1
          AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
          AND lt.nb_trades >= 5
          AND lt.entry_price_yes BETWEEN 0.05 AND 0.10
          AND m.volume >= 1000
          AND {kw_filter}
        ORDER BY m.end_date
    """).df()
    df["win_rate_prior"] = 0.975
    logger.info(f"  -> {len(df):,} trades")
    return df


def get_trades_s4_nothing_20pct(con) -> pd.DataFrame:
    """S4 : Parier NO quand le dernier prix YES est entre 10% et 20%."""
    logger.info("Extraction S4 : Nothing Ever Happens YES 10-20%...")
    kw_filter = " AND ".join([f"LOWER(m.question) NOT LIKE '%{k}%'" for k in CRYPTO_KEYWORDS])
    df = con.execute(f"""
        WITH last_trade AS (
            SELECT market_id,
                   LAST(price ORDER BY timestamp) AS entry_price_yes,
                   COUNT(*)                        AS nb_trades
            FROM read_parquet('{QUANT}')
            GROUP BY market_id
        )
        SELECT
            m.id         AS market_id,
            m.question,
            lt.entry_price_yes,
            CAST(SUBSTRING(m.outcome_prices,3,1) AS INTEGER) AS outcome,
            m.end_date   AS timestamp
        FROM read_parquet('{MARKETS}') m
        JOIN last_trade lt ON lt.market_id = m.id
        WHERE m.closed = 1
          AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
          AND lt.nb_trades >= 5
          AND lt.entry_price_yes BETWEEN 0.10 AND 0.20
          AND m.volume >= 1000
          AND {kw_filter}
        ORDER BY m.end_date
    """).df()
    df["win_rate_prior"] = 0.940
    logger.info(f"  -> {len(df):,} trades")
    return df


def get_trades_s5_impossible(con) -> pd.DataFrame:
    """S5 : Événements physiquement impossibles (Jesus, Aliens, WW3, etc.)."""
    logger.info("Extraction S5 : Evenements impossibles...")
    kw_filter = " OR ".join([f"LOWER(m.question) LIKE '%{k}%'" for k in IMPOSSIBLE_KEYWORDS])
    df = con.execute(f"""
        WITH last_trade AS (
            SELECT market_id,
                   LAST(price ORDER BY timestamp)   AS entry_price_yes,
                   COUNT(*)                          AS nb_trades
            FROM read_parquet('{QUANT}')
            GROUP BY market_id
        )
        SELECT
            m.id         AS market_id,
            m.question,
            COALESCE(lt.entry_price_yes, 0.05)     AS entry_price_yes,
            CAST(SUBSTRING(m.outcome_prices,3,1) AS INTEGER) AS outcome,
            m.end_date   AS timestamp
        FROM read_parquet('{MARKETS}') m
        LEFT JOIN last_trade lt ON lt.market_id = m.id
        WHERE m.closed = 1
          AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
          AND m.volume >= 500
          AND ({kw_filter})
          -- Garder uniquement ceux dont le prix YES est plausible (pas déjà à 0)
          AND COALESCE(lt.entry_price_yes, 0.05) > 0.001
        ORDER BY m.end_date
    """).df()
    df["win_rate_prior"] = 0.999  # Quasi-certitude
    logger.info(f"  -> {len(df):,} trades")
    if len(df) > 0:
        logger.info("Exemples de marches impossibles :")
        for _, row in df.head(5).iterrows():
            logger.info(f"  {row['question'][:70]:70s} prix_yes={row['entry_price_yes']:.3f} outcome={row['outcome']}")
    return df


def get_trades_s6_long_low_volume(con) -> pd.DataFrame:
    """S6 : Marchés de longue durée (> 30 jours) et faible volume (< 10K$)."""
    logger.info("Extraction S6 : Long duration + faible volume...")
    df = con.execute(f"""
        WITH last_trade AS (
            SELECT market_id,
                   LAST(price ORDER BY timestamp)   AS entry_price_yes,
                   COUNT(*)                          AS nb_trades
            FROM read_parquet('{QUANT}')
            GROUP BY market_id
        )
        SELECT
            m.id         AS market_id,
            m.question,
            lt.entry_price_yes,
            CAST(SUBSTRING(m.outcome_prices,3,1) AS INTEGER) AS outcome,
            m.end_date   AS timestamp
        FROM read_parquet('{MARKETS}') m
        JOIN last_trade lt ON lt.market_id = m.id
        WHERE m.closed = 1
          AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
          AND lt.nb_trades >= 5
          AND lt.entry_price_yes BETWEEN 0.05 AND 0.95
          AND m.volume BETWEEN 100 AND 10000
          AND DATE_DIFF('day', m.created_at::DATE, m.end_date::DATE) >= 30
        ORDER BY m.end_date
    """).df()
    df["win_rate_prior"] = 0.83
    logger.info(f"  -> {len(df):,} trades")
    return df


def get_trades_s7_volume_drop(con) -> pd.DataFrame:
    """S7 : Volume décroissant dans les 48h avant résolution (signal 89.7% NO)."""
    logger.info("Extraction S7 : Volume decroissant avant resolution...")
    df = con.execute(f"""
        WITH daily_vol AS (
            SELECT
                q.market_id,
                epoch_ms(q.timestamp::BIGINT*1000)::DATE     AS jour,
                SUM(q.usd_amount)                             AS vol_jour,
                AVG(q.price)                                  AS prix_moyen_jour
            FROM read_parquet('{QUANT}') q
            GROUP BY q.market_id, epoch_ms(q.timestamp::BIGINT*1000)::DATE
        ),
        market_signal AS (
            SELECT
                dv.market_id,
                -- Volume dans les 48h avant la fin
                MAX(CASE WHEN DATE_DIFF('day', dv.jour, m.end_date::DATE) <= 2
                         THEN dv.vol_jour ELSE NULL END)      AS vol_last_2d,
                -- Volume moyen journalier du marché
                AVG(dv.vol_jour)                              AS vol_avg,
                -- Prix d'entrée = dernier prix disponible
                LAST(dv.prix_moyen_jour ORDER BY dv.jour)    AS entry_price_yes,
                SUBSTRING(m.outcome_prices,3,1)              AS resultat
            FROM daily_vol dv
            JOIN read_parquet('{MARKETS}') m ON m.id = dv.market_id
            WHERE m.closed = 1
              AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
              AND m.volume >= 1000
            GROUP BY dv.market_id, SUBSTRING(m.outcome_prices,3,1)
            HAVING vol_avg > 0 AND vol_last_2d IS NOT NULL
        )
        SELECT
            market_id,
            '' AS question,
            entry_price_yes,
            CAST(resultat AS INTEGER)  AS outcome,
            CURRENT_DATE               AS timestamp
        FROM market_signal
        -- Signal : volume en baisse (< 80% de la moyenne)
        WHERE vol_last_2d < vol_avg * 0.80
          AND entry_price_yes BETWEEN 0.05 AND 0.95
        ORDER BY market_id
    """).df()
    df["win_rate_prior"] = 0.897
    logger.info(f"  -> {len(df):,} trades")
    return df


# ── Visualisation des courbes d'equity ────────────────────────────────────────

def plot_equity_curves(all_results: dict):
    """Compare les courbes d'equity de toutes les stratégies."""
    fig, axes = plt.subplots(2, 1, figsize=(14, 10))
    fig.suptitle("Backtest — Courbes d'equity par stratégie (capital initial : 1000$)", fontsize=13)

    colors = ["steelblue", "green", "limegreen", "olive", "purple", "coral", "orange"]

    for i, (name, metrics) in enumerate(all_results.items()):
        if "error" in metrics or not metrics.get("equity_curve"):
            continue
        eq = metrics["equity_curve"]
        color = colors[i % len(colors)]
        final = eq[-1]
        ret   = metrics["total_return_pct"]
        label = f"{name} ({ret:+.1f}%)"
        # Normaliser : courbe en % du capital initial
        eq_norm = [v / INITIAL_CAPITAL * 100 for v in eq]
        axes[0].plot(range(len(eq_norm)), eq_norm, color=color, linewidth=1.5, label=label, alpha=0.85)

    axes[0].axhline(100, color="black", linestyle="--", linewidth=1, label="Pas de gain")
    axes[0].set_xlabel("Trades (chronologiques)")
    axes[0].set_ylabel("Capital (% initial)")
    axes[0].set_title("Évolution du capital")
    axes[0].legend(fontsize=8, loc="upper left")

    # Graphique en barres du rendement total
    names   = [n for n, m in all_results.items() if "error" not in m]
    returns = [all_results[n]["total_return_pct"] for n in names]
    bar_colors = ["green" if r > 0 else "red" for r in returns]

    axes[1].barh(names, returns, color=bar_colors)
    axes[1].axvline(0, color="black", linewidth=1)
    axes[1].set_xlabel("Rendement total (%)")
    axes[1].set_title("Rendement total par stratégie")
    for i, (n, r) in enumerate(zip(names, returns)):
        axes[1].text(r + (1 if r >= 0 else -1), i, f"{r:+.1f}%", va="center",
                     ha="left" if r >= 0 else "right", fontsize=9)

    plt.tight_layout()
    save_fig(fig, "equity_curves")


def plot_scorecard(all_results: dict):
    """Tableau de bord synthétique de toutes les stratégies."""
    rows = []
    for name, m in all_results.items():
        if "error" in m:
            continue
        rows.append([
            name,
            f"{m['nb_trades']:,}",
            f"{m['win_rate_pct']:.1f}%",
            f"{m['total_return_pct']:+.1f}%",
            f"{m['max_drawdown_pct']:.1f}%",
            f"{m['profit_factor']:.2f}",
            f"{m['sharpe_approx']:.2f}",
        ])

    if not rows:
        return

    fig, ax = plt.subplots(figsize=(14, len(rows) * 0.7 + 2))
    ax.axis("off")
    headers = ["Stratégie", "Nb trades", "Win rate", "Rendement", "Max DD", "Profit F.", "Sharpe"]
    tbl = ax.table(cellText=rows, colLabels=headers, loc="center", cellLoc="center")
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, 1.6)

    # Colorier la colonne Rendement (vert/rouge)
    for i, row in enumerate(rows):
        ret = float(row[3].replace("%", "").replace("+", ""))
        color = "#c8f7c5" if ret > 0 else "#f7c5c5"
        tbl[i + 1, 3].set_facecolor(color)
        wr = float(row[2].replace("%", ""))
        tbl[i + 1, 2].set_facecolor("#c8f7c5" if wr >= 75 else "#fff3c5" if wr >= 60 else "#f7c5c5")

    ax.set_title("Scorecard Backtest — Phase 4", fontsize=13, pad=20)
    plt.tight_layout()
    save_fig(fig, "backtest_scorecard")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    log_file = setup()
    logger.info("=" * 60)
    logger.info("PHASE 4 — Backtest complet (7 strategies)")
    logger.info(f"Capital initial : {INITIAL_CAPITAL}$ | Kelly fraction : 25%")
    logger.info("=" * 60)

    con = duckdb.connect()
    con.execute("SET threads = 4; SET memory_limit = '4GB'")

    # ── Extraction des trades ─────────────────────────────────────────────────
    logger.info("\n--- Extraction des trades par strategie ---")
    strategies = {
        "S1 NO Global":          (get_trades_s1_no_global,    0.672),
        "S2 YES lt5pct":         (get_trades_s2_nothing_5pct, 0.999),
        "S3 YES 5-10%":          (get_trades_s3_nothing_10pct,0.975),
        "S4 YES 10-20%":         (get_trades_s4_nothing_20pct,0.940),
        "S5 Impossible Events":  (get_trades_s5_impossible,   0.999),
        "S6 Long+Low Vol":       (get_trades_s6_long_low_volume, 0.830),
        "S7 Volume Drop":        (get_trades_s7_volume_drop,  0.897),
    }

    all_results = {}

    for name, (getter, _) in strategies.items():
        logger.info(f"\n{'='*40}")
        logger.info(f"Strategie : {name}")
        try:
            trades_df = getter(con)
            if trades_df.empty:
                logger.warning(f"Aucun trade pour {name}")
                all_results[name] = {"error": "Aucun trade"}
                continue
            metrics = simulate_trades(trades_df, initial_capital=INITIAL_CAPITAL)
            all_results[name] = metrics
            print_metrics(name, metrics)

            # Sauvegarder les trades détaillés
            if "trades_df" in metrics:
                safe = name.replace(' ', '_').replace('/', '_').replace('<', 'lt').replace('>', 'gt')
                metrics["trades_df"].to_csv(OUT_DIR / f"trades_{safe}.csv", index=False)
        except Exception as e:
            logger.error(f"Erreur {name} : {e}")
            import traceback
            logger.error(traceback.format_exc())
            all_results[name] = {"error": str(e)}

    con.close()

    # ── Visualisations ────────────────────────────────────────────────────────
    logger.info("\n--- Génération des graphiques ---")
    plot_equity_curves(all_results)
    plot_scorecard(all_results)

    # ── Résumé final ──────────────────────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    logger.info("RESUME FINAL DU BACKTEST")
    logger.info("=" * 60)
    for name, m in all_results.items():
        if "error" not in m:
            logger.info(
                f"  {name:25s} | {m['nb_trades']:>7,} trades "
                f"| WR {m['win_rate_pct']:>5.1f}% "
                f"| Rendement {m['total_return_pct']:>+8.1f}% "
                f"| DD {m['max_drawdown_pct']:>6.1f}% "
                f"| Sharpe {m['sharpe_approx']:>5.2f}"
            )

    logger.info(f"\nOutputs : {OUT_DIR.relative_to(PROJECT_ROOT)}")
    logger.info(f"Log : {log_file}")
    logger.success("Backtest termine.")


if __name__ == "__main__":
    main()
