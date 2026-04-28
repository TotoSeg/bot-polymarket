"""
Phase 4 — Deep Test Stratégie S3 (YES 5-10%)
==============================================
Batterie complète de tests sur la stratégie S3 "Nothing Ever Happens 5-10%".
S3 = parier NO quand le dernier prix YES avant résolution est entre 5% et 10%.

Tests effectués :
  T1  - Sous-buckets de prix (5-6%, 6-7%, 7-8%, 8-9%, 9-10%)
  T2  - Bandes de volume (< 5K$, 5-20K$, 20-100K$, > 100K$)
  T3  - Durée du marché (< 7j, 7-30j, 30-90j, > 90j)
  T4  - Stabilité temporelle par année (2022, 2023, 2024, 2025)
  T5  - Stabilité temporelle par trimestre
  T6  - Catégories de marchés (politique, sport, entertainment, etc.)
  T7  - Sensibilité au Kelly fraction (5%, 10%, 25%, 50%, 100%)
  T8  - Walk-forward validation (première moitié train, deuxième moitié test)
  T9  - Intervalles de confiance bootstrap sur le win rate
  T10 - Impact du filtre volume minimum (seuils 0$, 500$, 1K$, 5K$, 10K$)

Usage :
    python src/phase4_backtest/backtest_s3_deep.py
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
import matplotlib.gridspec as gridspec
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.phase4_backtest.backtest_engine import simulate_trades, POLYMARKET_FEE

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR     = PROJECT_ROOT / "data"
LOGS_DIR     = PROJECT_ROOT / "logs"
OUT_DIR      = PROJECT_ROOT / "outputs" / "phase4" / "s3_deep"

MARKETS = str(DATA_DIR / "markets.parquet")
QUANT   = str(DATA_DIR / "quant.parquet")

INITIAL_CAPITAL = 1000.0

# Mots-clés crypto à exclure
CRYPTO_KEYWORDS = [
    "bitcoin", " btc", "ethereum", " eth", "solana", "xrp", "crypto",
    "up or down", "updown",
]


def setup():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_s3_deep.log"
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


# ── Extraction de la base S3 complète ─────────────────────────────────────────

def load_s3_base(con) -> pd.DataFrame:
    """
    Charge TOUS les marchés S3 avec métadonnées complètes.
    Colonnes extra vs backtest standard : volume, end_date, created_at,
    duration_days, year, quarter, category_kw.
    """
    kw_filter = " AND ".join([f"LOWER(m.question) NOT LIKE '%{k}%'" for k in CRYPTO_KEYWORDS])

    logger.info("Chargement de la base S3 complète...")
    df = con.execute(f"""
        WITH last_trade AS (
            SELECT market_id,
                   LAST(price ORDER BY timestamp)  AS entry_price_yes,
                   COUNT(*)                         AS nb_trades
            FROM read_parquet('{QUANT}')
            GROUP BY market_id
        )
        SELECT
            m.id                                                            AS market_id,
            m.question,
            lt.entry_price_yes,
            CAST(SUBSTRING(m.outcome_prices,3,1) AS INTEGER)               AS outcome,
            m.end_date,
            m.created_at,
            ROUND(m.volume, 2)                                              AS volume,
            YEAR(m.end_date::DATE)                                          AS year,
            QUARTER(m.end_date::DATE)                                       AS quarter,
            DATE_DIFF('day', m.created_at::DATE, m.end_date::DATE)         AS duration_days
        FROM read_parquet('{MARKETS}') m
        JOIN last_trade lt ON lt.market_id = m.id
        WHERE m.closed = 1
          AND SUBSTRING(m.outcome_prices,3,1) IN ('0','1')
          AND lt.nb_trades >= 5
          AND lt.entry_price_yes BETWEEN 0.05 AND 0.10
          AND m.volume >= 100
          AND {kw_filter}
        ORDER BY m.end_date
    """).df()

    df["win_rate_prior"] = 0.975
    logger.info(f"Base S3 : {len(df):,} marchés")
    return df


def quick_metrics(df: pd.DataFrame, label: str = "") -> dict:
    """Lance le backtest et retourne un résumé compact."""
    if len(df) == 0:
        return {"label": label, "n": 0, "wr": np.nan, "ret": np.nan,
                "dd": np.nan, "sharpe": np.nan, "pf": np.nan}
    m = simulate_trades(df, initial_capital=INITIAL_CAPITAL)
    if "error" in m:
        return {"label": label, "n": 0, "wr": np.nan, "ret": np.nan,
                "dd": np.nan, "sharpe": np.nan, "pf": np.nan}
    return {
        "label":  label,
        "n":      m["nb_trades"],
        "wr":     m["win_rate_pct"],
        "ret":    m["total_return_pct"],
        "dd":     m["max_drawdown_pct"],
        "sharpe": m["sharpe_approx"],
        "pf":     m["profit_factor"],
    }


def print_table(rows: list[dict], title: str):
    """Affiche un tableau formaté dans les logs."""
    logger.info(f"\n{'─'*70}")
    logger.info(f"  {title}")
    logger.info(f"{'─'*70}")
    logger.info(f"  {'Segment':30s} {'N':>7} {'WR%':>7} {'Ret%':>9} "
                f"{'MaxDD%':>8} {'Sharpe':>7} {'PF':>6}")
    logger.info(f"  {'─'*65}")
    for r in rows:
        if r["n"] == 0:
            logger.info(f"  {r['label']:30s} {'N/A':>7}")
            continue
        logger.info(
            f"  {r['label']:30s} {r['n']:>7,} {r['wr']:>6.1f}% "
            f"{r['ret']:>+8.1f}% {r['dd']:>7.1f}% "
            f"{r['sharpe']:>7.2f} {r['pf']:>6.2f}"
        )


# ── T1 : Sous-buckets de prix ──────────────────────────────────────────────────

def test_t1_price_buckets(df: pd.DataFrame) -> list[dict]:
    logger.info("\n=== T1 : Sous-buckets de prix (5-6%, 6-7%, ..., 9-10%) ===")
    buckets = [
        ("5.0-6.0%", 0.050, 0.060),
        ("6.0-7.0%", 0.060, 0.070),
        ("7.0-8.0%", 0.070, 0.080),
        ("8.0-9.0%", 0.080, 0.090),
        ("9.0-10.0%", 0.090, 0.100),
    ]
    rows = []
    for label, lo, hi in buckets:
        sub = df[df["entry_price_yes"].between(lo, hi)].copy()
        rows.append(quick_metrics(sub, label))

    print_table(rows, "T1 — Win rate par tranche de prix YES")
    return rows


# ── T2 : Bandes de volume ─────────────────────────────────────────────────────

def test_t2_volume_bands(df: pd.DataFrame) -> list[dict]:
    logger.info("\n=== T2 : Bandes de volume ===")
    bands = [
        ("Vol < 1K$",        0,       1_000),
        ("Vol 1-5K$",        1_000,   5_000),
        ("Vol 5-20K$",       5_000,  20_000),
        ("Vol 20-100K$",    20_000, 100_000),
        ("Vol > 100K$",    100_000,  np.inf),
    ]
    rows = []
    for label, lo, hi in bands:
        sub = df[(df["volume"] >= lo) & (df["volume"] < hi)].copy()
        rows.append(quick_metrics(sub, label))

    print_table(rows, "T2 — Win rate par volume du marché")
    return rows


# ── T3 : Durée du marché ──────────────────────────────────────────────────────

def test_t3_duration(df: pd.DataFrame) -> list[dict]:
    logger.info("\n=== T3 : Durée du marché ===")
    bands = [
        ("Durée < 7j",   -np.inf,  7),
        ("Durée 7-30j",       7, 30),
        ("Durée 30-90j",     30, 90),
        ("Durée > 90j",      90, np.inf),
    ]
    rows = []
    for label, lo, hi in bands:
        sub = df[(df["duration_days"] >= lo) & (df["duration_days"] < hi)].copy()
        rows.append(quick_metrics(sub, label))

    print_table(rows, "T3 — Win rate par durée du marché")
    return rows


# ── T4 : Stabilité par année ──────────────────────────────────────────────────

def test_t4_yearly(df: pd.DataFrame) -> list[dict]:
    logger.info("\n=== T4 : Stabilité temporelle par année ===")
    rows = []
    for yr in sorted(df["year"].dropna().unique()):
        sub = df[df["year"] == yr].copy()
        rows.append(quick_metrics(sub, f"Année {int(yr)}"))

    # Ajout : baseline global
    rows.append(quick_metrics(df, "TOTAL (baseline)"))
    print_table(rows, "T4 — Win rate par année")
    return rows


# ── T5 : Stabilité par trimestre ──────────────────────────────────────────────

def test_t5_quarterly(df: pd.DataFrame) -> list[dict]:
    logger.info("\n=== T5 : Stabilité par trimestre ===")
    df2 = df.dropna(subset=["year", "quarter"]).copy()
    df2["yq"] = df2["year"].astype(int).astype(str) + "-Q" + df2["quarter"].astype(int).astype(str)
    rows = []
    for yq in sorted(df2["yq"].unique()):
        sub = df2[df2["yq"] == yq].copy()
        rows.append(quick_metrics(sub, yq))

    print_table(rows, "T5 — Win rate par trimestre")
    return rows


# ── T6 : Catégories de marchés ────────────────────────────────────────────────

def test_t6_categories(df: pd.DataFrame) -> list[dict]:
    logger.info("\n=== T6 : Catégories de marchés ===")

    # Règles de catégorisation par mots-clés (priorité décroissante)
    CATEGORIES = [
        ("Politique US",    ["president", "trump", "biden", "harris", "congress", "senate",
                              "election", "vote", "democrat", "republican", "white house"]),
        ("Politique monde", ["parliament", "prime minister", "chancellor", "president",
                              "war", "ceasefire", "nato", "sanction", "coup"]),
        ("Sport",           ["nfl", "nba", "mlb", "nhl", "fifa", "world cup", "champion",
                              "super bowl", "playoffs", "finals", "match", "game", "win",
                              "league", "tournament", "ufc", "boxing"]),
        ("Entertainment",   ["oscar", "grammy", "emmy", "award", "movie", "album",
                              "spotify", "film", "actor", "singer", "celebrity",
                              "kardashian", "taylor swift", "beyonce"]),
        ("Economie",        ["fed", "inflation", "gdp", "recession", "rate", "dollar",
                              "market cap", "nasdaq", "s&p", "dow jones", "ipo"]),
        ("Tech",            ["ai", "openai", "gpt", "apple", "google", "meta", "microsoft",
                              "amazon", "tesla", "spacex", "x.com", "elon"]),
        ("Autres",          []),  # Catch-all
    ]

    rows = []
    df2 = df.copy()
    df2["_assigned"] = False

    for cat_name, keywords in CATEGORIES:
        if not keywords:
            # Catch-all
            sub = df2[~df2["_assigned"]].copy()
            rows.append(quick_metrics(sub, f"Autres (catch-all)"))
            break
        mask = df2["question"].str.lower().str.contains("|".join(keywords), na=False)
        mask &= ~df2["_assigned"]
        sub = df2[mask].copy()
        df2.loc[mask, "_assigned"] = True
        rows.append(quick_metrics(sub, cat_name))

    print_table(rows, "T6 — Win rate par catégorie")
    return rows


# ── T7 : Sensibilité au Kelly fraction ────────────────────────────────────────

def test_t7_kelly_sensitivity(df: pd.DataFrame) -> list[dict]:
    """
    Teste l'impact du Kelly fraction sur les performances.
    On modifie directement KELLY_FRACTION dans l'engine.
    """
    import src.phase4_backtest.backtest_engine as engine

    logger.info("\n=== T7 : Sensibilité au Kelly fraction ===")
    fractions = [0.05, 0.10, 0.25, 0.50, 1.00]
    rows = []
    original = engine.KELLY_FRACTION

    for frac in fractions:
        engine.KELLY_FRACTION = frac
        m = simulate_trades(df.copy(), initial_capital=INITIAL_CAPITAL)
        if "error" not in m:
            rows.append({
                "label":  f"Kelly {int(frac*100)}%",
                "n":      m["nb_trades"],
                "wr":     m["win_rate_pct"],
                "ret":    m["total_return_pct"],
                "dd":     m["max_drawdown_pct"],
                "sharpe": m["sharpe_approx"],
                "pf":     m["profit_factor"],
            })

    engine.KELLY_FRACTION = original  # Restaurer
    print_table(rows, "T7 — Impact du Kelly fraction")
    return rows


# ── T8 : Walk-forward validation ──────────────────────────────────────────────

def test_t8_walk_forward(df: pd.DataFrame) -> list[dict]:
    """
    Découpe les données en deux moitiés chronologiques :
    - 1ère moitié : "in-sample" (ce qu'on a analysé en phase 3)
    - 2ème moitié : "out-of-sample" (validation réelle)
    """
    logger.info("\n=== T8 : Walk-forward validation ===")
    df_sorted = df.sort_values("end_date").reset_index(drop=True)
    mid = len(df_sorted) // 2

    in_sample  = df_sorted.iloc[:mid].copy()
    out_sample = df_sorted.iloc[mid:].copy()

    # 4 fenêtres glissantes de 25%
    q1 = df_sorted.iloc[: mid//2].copy()
    q2 = df_sorted.iloc[mid//2 : mid].copy()
    q3 = df_sorted.iloc[mid : mid + (len(df_sorted)-mid)//2].copy()
    q4 = df_sorted.iloc[mid + (len(df_sorted)-mid)//2 :].copy()

    rows = [
        quick_metrics(in_sample,  "In-sample (1ère moitié)"),
        quick_metrics(out_sample, "Out-of-sample (2ème moitié)"),
        quick_metrics(q1, "Fenêtre 1 (25%)"),
        quick_metrics(q2, "Fenêtre 2 (25%)"),
        quick_metrics(q3, "Fenêtre 3 (25%)"),
        quick_metrics(q4, "Fenêtre 4 (25%)"),
    ]
    print_table(rows, "T8 — Walk-forward validation")
    return rows


# ── T9 : Bootstrap sur le win rate ────────────────────────────────────────────

def test_t9_bootstrap(df: pd.DataFrame, n_bootstrap: int = 1000) -> dict:
    """
    Bootstraps le win rate pour estimer l'intervalle de confiance à 95%.
    Si le bas de l'IC est > 90%, la stratégie est robuste.
    """
    logger.info(f"\n=== T9 : Bootstrap win rate (n={n_bootstrap:,} itérations) ===")
    outcomes = df["outcome"].values  # 0 = NO gagne = win, 1 = YES gagne = loss

    # Win = outcome == 0
    wins = (outcomes == 0)
    bootstrap_wrs = []
    rng = np.random.default_rng(42)

    for _ in range(n_bootstrap):
        sample = rng.choice(wins, size=len(wins), replace=True)
        bootstrap_wrs.append(sample.mean() * 100)

    bootstrap_wrs = np.array(bootstrap_wrs)
    observed_wr = wins.mean() * 100
    ci_lo = np.percentile(bootstrap_wrs, 2.5)
    ci_hi = np.percentile(bootstrap_wrs, 97.5)

    logger.info(f"  Win rate observé : {observed_wr:.2f}%")
    logger.info(f"  IC 95% bootstrap : [{ci_lo:.2f}%, {ci_hi:.2f}%]")
    logger.info(f"  Nb marchés       : {len(df):,}")

    # Intervalle de confiance par année
    logger.info("  IC par année :")
    for yr in sorted(df["year"].dropna().unique()):
        sub_wins = (df[df["year"] == yr]["outcome"] == 0).values
        if len(sub_wins) < 10:
            continue
        yr_wr = sub_wins.mean() * 100
        yr_bst = []
        for _ in range(500):
            s = rng.choice(sub_wins, size=len(sub_wins), replace=True)
            yr_bst.append(s.mean() * 100)
        lo = np.percentile(yr_bst, 2.5)
        hi = np.percentile(yr_bst, 97.5)
        logger.info(f"    {int(yr)} : WR={yr_wr:.1f}% IC95=[{lo:.1f}%, {hi:.1f}%] (n={len(sub_wins)})")

    return {"observed_wr": observed_wr, "ci_lo": ci_lo, "ci_hi": ci_hi,
            "bootstrap_wrs": bootstrap_wrs}


# ── T10 : Impact du filtre volume minimum ─────────────────────────────────────

def test_t10_volume_threshold(df_all: pd.DataFrame) -> list[dict]:
    """
    Teste différents seuils de volume minimum pour voir l'impact sur
    le nombre de trades et le win rate.
    """
    logger.info("\n=== T10 : Impact du seuil de volume minimum ===")
    thresholds = [0, 100, 500, 1_000, 5_000, 10_000, 50_000]
    rows = []
    for thresh in thresholds:
        sub = df_all[df_all["volume"] >= thresh].copy()
        rows.append(quick_metrics(sub, f"Vol >= {thresh:>7,.0f}$"))

    print_table(rows, "T10 — Impact du filtre volume minimum")
    return rows


# ── Visualisation synthétique ─────────────────────────────────────────────────

def plot_all(t1, t2, t3, t4, t5, t7, t9, t10, df_base):
    """Génère un tableau de bord complet pour S3."""

    # ── Figure 1 : Win rate par segment (T1, T2, T3, T6) ────────────────────
    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.suptitle("S3 (YES 5-10%) — Analyse par segment", fontsize=14)

    def bar_wr(ax, rows, title, color="steelblue"):
        labels = [r["label"] for r in rows if r["n"] > 0]
        wrs    = [r["wr"]    for r in rows if r["n"] > 0]
        ns     = [r["n"]     for r in rows if r["n"] > 0]
        colors = ["green" if w >= 95 else "orange" if w >= 85 else "red" for w in wrs]
        bars = ax.bar(range(len(labels)), wrs, color=colors)
        ax.axhline(97.5, color="gray", linestyle="--", linewidth=1, label="Baseline 97.5%")
        ax.axhline(90,   color="orange", linestyle=":", linewidth=1, label="Seuil 90%")
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
        ax.set_ylabel("Win rate (%)")
        ax.set_title(title)
        ax.set_ylim(50, 102)
        ax.legend(fontsize=7)
        for i, (w, n) in enumerate(zip(wrs, ns)):
            ax.text(i, w + 0.3, f"{w:.1f}%\nn={n}", ha="center", fontsize=6)

    bar_wr(axes[0][0], t1, "T1 — Par tranche de prix")
    bar_wr(axes[0][1], t2, "T2 — Par volume")
    bar_wr(axes[1][0], t3, "T3 — Par durée")
    bar_wr(axes[1][1], t4, "T4 — Par année")

    plt.tight_layout()
    save_fig(fig, "s3_win_rates_segments")

    # ── Figure 2 : Bootstrap distribution ────────────────────────────────────
    if t9 and "bootstrap_wrs" in t9:
        fig2, ax2 = plt.subplots(figsize=(10, 5))
        ax2.hist(t9["bootstrap_wrs"], bins=50, color="steelblue", edgecolor="white", alpha=0.8)
        ax2.axvline(t9["observed_wr"], color="red", linewidth=2, label=f"Observé : {t9['observed_wr']:.1f}%")
        ax2.axvline(t9["ci_lo"], color="orange", linestyle="--", linewidth=1.5,
                    label=f"IC 95% : [{t9['ci_lo']:.1f}%, {t9['ci_hi']:.1f}%]")
        ax2.axvline(t9["ci_hi"], color="orange", linestyle="--", linewidth=1.5)
        ax2.axvline(90, color="black", linestyle=":", label="Seuil 90%")
        ax2.set_xlabel("Win rate (%)")
        ax2.set_ylabel("Fréquence (bootstrap)")
        ax2.set_title("T9 — Distribution bootstrap du win rate S3 (1 000 itérations)")
        ax2.legend()
        plt.tight_layout()
        save_fig(fig2, "s3_bootstrap")

    # ── Figure 3 : Sensibilité Kelly + Rendement/Drawdown ─────────────────────
    if t7:
        fig3, axes3 = plt.subplots(1, 3, figsize=(16, 5))
        fig3.suptitle("T7 — Sensibilité au Kelly fraction", fontsize=12)

        labels = [r["label"] for r in t7]
        rets   = [r["ret"]   for r in t7]
        dds    = [r["dd"]    for r in t7]
        shrps  = [r["sharpe"] for r in t7]

        axes3[0].bar(labels, rets, color=["green" if r > 0 else "red" for r in rets])
        axes3[0].set_title("Rendement total (%)")
        axes3[0].set_ylabel("%")

        axes3[1].bar(labels, dds, color="salmon")
        axes3[1].set_title("Max Drawdown (%)")
        axes3[1].set_ylabel("%")

        axes3[2].bar(labels, shrps, color="steelblue")
        axes3[2].set_title("Sharpe ratio")
        plt.tight_layout()
        save_fig(fig3, "s3_kelly_sensitivity")

    # ── Figure 4 : Évolution du win rate trimestriel ─────────────────────────
    if t5:
        valid_t5 = [r for r in t5 if r["n"] >= 5]
        if valid_t5:
            fig4, ax4 = plt.subplots(figsize=(14, 5))
            xs     = range(len(valid_t5))
            labels = [r["label"] for r in valid_t5]
            wrs    = [r["wr"]    for r in valid_t5]
            ns     = [r["n"]     for r in valid_t5]

            ax4.plot(xs, wrs, marker="o", color="steelblue", linewidth=2, markersize=6)
            ax4.fill_between(xs, [90]*len(xs), wrs,
                             where=[w >= 90 for w in wrs], alpha=0.15, color="green")
            ax4.fill_between(xs, [90]*len(xs), wrs,
                             where=[w < 90 for w in wrs], alpha=0.15, color="red")
            ax4.axhline(97.5, color="gray", linestyle="--", linewidth=1, label="Baseline 97.5%")
            ax4.axhline(90,   color="orange", linestyle=":", linewidth=1, label="Seuil 90%")
            ax4.set_xticks(list(xs))
            ax4.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
            ax4.set_ylim(50, 102)
            ax4.set_ylabel("Win rate (%)")
            ax4.set_title("T5 — Évolution du win rate S3 par trimestre")
            ax4.legend()
            for x, w, n in zip(xs, wrs, ns):
                ax4.annotate(f"{w:.0f}%\n(n={n})", (x, w), textcoords="offset points",
                             xytext=(0, 8), ha="center", fontsize=7)
            plt.tight_layout()
            save_fig(fig4, "s3_quarterly_winrate")

    # ── Figure 5 : Volume threshold impact ───────────────────────────────────
    if t10:
        valid_t10 = [r for r in t10 if r["n"] > 0]
        fig5, axes5 = plt.subplots(1, 2, figsize=(12, 5))
        fig5.suptitle("T10 — Impact du seuil de volume minimum", fontsize=12)

        labels = [r["label"] for r in valid_t10]
        xs     = range(len(labels))
        wrs    = [r["wr"]  for r in valid_t10]
        ns     = [r["n"]   for r in valid_t10]

        axes5[0].bar(xs, wrs, color=["green" if w >= 95 else "orange" for w in wrs])
        axes5[0].set_xticks(list(xs))
        axes5[0].set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
        axes5[0].axhline(97.5, color="gray", linestyle="--")
        axes5[0].set_ylim(90, 100)
        axes5[0].set_title("Win rate (%)")

        axes5[1].bar(xs, ns, color="steelblue")
        axes5[1].set_xticks(list(xs))
        axes5[1].set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
        axes5[1].set_title("Nombre de trades disponibles")

        plt.tight_layout()
        save_fig(fig5, "s3_volume_threshold")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    log_file = setup()
    logger.info("=" * 70)
    logger.info("PHASE 4 — Deep Test S3 : Nothing Ever Happens YES 5-10%")
    logger.info("=" * 70)

    con = duckdb.connect()
    con.execute("SET threads = 4; SET memory_limit = '4GB'")

    # Chargement de la base
    df = load_s3_base(con)
    con.close()

    if len(df) == 0:
        logger.error("Aucun trade S3 trouvé. Vérifier les fichiers Parquet.")
        return

    # Run des tests
    t1  = test_t1_price_buckets(df)
    t2  = test_t2_volume_bands(df)
    t3  = test_t3_duration(df)
    t4  = test_t4_yearly(df)
    t5  = test_t5_quarterly(df)
    t6  = test_t6_categories(df)
    t7  = test_t7_kelly_sensitivity(df)
    t8  = test_t8_walk_forward(df)
    t9  = test_t9_bootstrap(df, n_bootstrap=2000)
    t10 = test_t10_volume_threshold(df)

    # Résumé des résultats + interprétation
    logger.info("\n" + "=" * 70)
    logger.info("SYNTHESE S3")
    logger.info("=" * 70)

    # Win rate minimal observé (excl. segments trop petits, n < 20)
    all_wrs = [r["wr"] for tests in [t1, t2, t3, t4, t5, t6, t7, t8]
               for r in tests if r["n"] >= 20 and not np.isnan(r["wr"])]
    if all_wrs:
        logger.info(f"  Win rate min (tous segments n>=20) : {min(all_wrs):.1f}%")
        logger.info(f"  Win rate max                       : {max(all_wrs):.1f}%")
        logger.info(f"  Win rate médian                    : {np.median(all_wrs):.1f}%")

    nb_above_90 = sum(1 for w in all_wrs if w >= 90.0)
    logger.info(f"  Segments avec WR >= 90%            : {nb_above_90}/{len(all_wrs)}")

    if t9:
        logger.info(f"  IC 95% bootstrap                   : [{t9['ci_lo']:.1f}%, {t9['ci_hi']:.1f}%]")

    # Robustesse walk-forward
    if t8 and len(t8) >= 2 and t8[0]["n"] > 0 and t8[1]["n"] > 0:
        in_wr  = t8[0]["wr"]
        out_wr = t8[1]["wr"]
        delta  = out_wr - in_wr
        logger.info(f"  Walk-forward : in={in_wr:.1f}% → out={out_wr:.1f}% (delta={delta:+.1f}%)")
        if abs(delta) < 5:
            logger.info("  -> ROBUSTE : pas de dégradation out-of-sample significative")
        else:
            logger.warning("  -> ATTENTION : dégradation out-of-sample significative")

    # Graphiques
    logger.info("\n--- Génération des graphiques ---")
    plot_all(t1, t2, t3, t4, t5, t7, t9, t10, df)

    # Export CSV
    summary_rows = []
    for test_name, rows in [("T1_prix", t1), ("T2_volume", t2), ("T3_duree", t3),
                              ("T4_annee", t4), ("T5_trim", t5), ("T6_categ", t6),
                              ("T7_kelly", t7), ("T8_wf", t8), ("T10_vthresh", t10)]:
        for r in rows:
            summary_rows.append({"test": test_name, **r})
    pd.DataFrame(summary_rows).to_csv(OUT_DIR / "s3_deep_summary.csv", index=False)

    logger.info(f"\nOutputs : {OUT_DIR.relative_to(PROJECT_ROOT)}")
    logger.success("Deep Test S3 termine.")


if __name__ == "__main__":
    main()
