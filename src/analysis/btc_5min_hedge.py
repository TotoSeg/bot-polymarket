"""
Analyse de hedge sur les marchés Bitcoin up/down 5 minutes
==========================================================

Objectif : Tester si miser sur le side majoritaire (upsider) ou minoritaire
(underdog) à différents moments avant la résolution génère un ROI positif.

Définitions :
  - Upsider  = side dont le prix est > 0.5 (plus probable de gagner)
  - Underdog = side dont le prix est < 0.5 (moins probable de gagner)
  - Si YES = 0.60 → upsider = YES,   underdog = NO
  - Si YES = 0.35 → upsider = NO,    underdog = YES

Métriques calculées par fenêtre temporelle :
  - win_rate_upsider   : % de fois que le side majoritaire gagne
  - roi_upsider        : rendement en % sur investissement
  - win_rate_underdog  : % de fois que le side minoritaire gagne
  - roi_underdog       : rendement en % sur investissement

ROI formule :
  Mise sur YES au prix P, taille 1$ :
    - Si YES gagne : gain net = (1/P - 1)$   → retour = 1/P
    - Si YES perd  : perte = -1$
  ROI = win_rate * (1/P - 1) - (1 - win_rate) * 1
      = win_rate / P - 1

Usage :
  python3 src/analysis/btc_5min_hedge.py
"""

import duckdb
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
from pathlib import Path
import sys

# ── Chemins ──────────────────────────────────────────────────────────────────
DATA_DIR     = Path(__file__).resolve().parents[2] / "data"
OUTPUT_DIR   = Path(__file__).resolve().parents[2] / "outputs" / "btc_hedge"
MARKETS_FILE = DATA_DIR / "markets.parquet"
QUANT_FILE   = DATA_DIR / "quant.parquet"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Fenêtres temporelles analysées (secondes avant résolution) ────────────
# Pas 30s de 5min à 2min, puis 10s de 2min à 1min, puis 5s de 1min à 0
WINDOWS_30S = list(range(300, 120 - 1, -30))   # 300, 270, 240, 210, 180, 150, 120
WINDOWS_10S = list(range(110, 60 - 1, -10))    # 110, 100, 90, 80, 70, 60
WINDOWS_5S  = list(range(55, 0 - 1, -5))       # 55, 50, 45, 40, 35, 30, 25, 20, 15, 10, 5, 0
ALL_WINDOWS = sorted(set(WINDOWS_30S + WINDOWS_10S + WINDOWS_5S), reverse=True)

print(f"Fenêtres analysées : {len(ALL_WINDOWS)} points temporels")
print(f"  Min: {min(ALL_WINDOWS)}s   Max: {max(ALL_WINDOWS)}s avant résolution")

# ── Connexion DuckDB ──────────────────────────────────────────────────────
con = duckdb.connect()

# ── Étape 1 : Identifier les marchés Bitcoin up/down 5 minutes ───────────
print("\n[1/4] Recherche des marchés Bitcoin up/down 5 minutes...")

btc_markets_query = f"""
SELECT
    condition_id,
    question,
    market_slug,
    end_date_iso,
    start_date_iso,
    resolution,
    volume_usd,
    -- Durée du marché en secondes
    CAST(EXTRACT(EPOCH FROM (CAST(end_date_iso AS TIMESTAMPTZ)
                             - CAST(start_date_iso AS TIMESTAMPTZ))) AS INTEGER) AS duration_sec
FROM read_parquet('{MARKETS_FILE}')
WHERE
    (
        lower(question) LIKE '%bitcoin%'
        OR lower(question) LIKE '% btc %'
        OR lower(market_slug) LIKE '%bitcoin%'
        OR lower(market_slug) LIKE '%btc%'
    )
    AND (
        lower(question) LIKE '%up%or%down%'
        OR lower(question) LIKE '%up or down%'
        OR lower(question) LIKE '%above%or%below%'
        OR lower(question) LIKE '%higher%or%lower%'
        OR lower(question) LIKE '%go up%'
        OR lower(question) LIKE '%go down%'
        OR lower(market_slug) LIKE '%up-or-down%'
        OR lower(market_slug) LIKE '%higher-or-lower%'
        OR lower(market_slug) LIKE '%above%below%'
    )
    AND end_date_iso IS NOT NULL
    AND start_date_iso IS NOT NULL
ORDER BY volume_usd DESC
"""

btc_markets = con.execute(btc_markets_query).df()

print(f"  Marchés Bitcoin up/down trouvés : {len(btc_markets)}")
if len(btc_markets) == 0:
    # Essai sans filtre up/down pour diagnostiquer
    fallback = con.execute(f"""
        SELECT question, market_slug, duration_sec
        FROM (
            SELECT question, market_slug,
                   CAST(EXTRACT(EPOCH FROM (CAST(end_date_iso AS TIMESTAMPTZ)
                                - CAST(start_date_iso AS TIMESTAMPTZ))) AS INTEGER) AS duration_sec
            FROM read_parquet('{MARKETS_FILE}')
            WHERE lower(question) LIKE '%bitcoin%' OR lower(market_slug) LIKE '%btc%'
        )
        WHERE duration_sec BETWEEN 60 AND 900
        LIMIT 20
    """).df()
    print("  Marchés BTC avec durée 1-15min :")
    print(fallback[["question", "duration_sec"]].to_string())
    sys.exit(1)

# Filtrer sur les marchés de ~5 minutes (240-480s pour tolérance)
# et afficher la distribution des durées
print(f"\n  Distribution des durées (secondes) :")
print(btc_markets["duration_sec"].describe().to_string())
print(f"\n  Exemples de marchés (top 5 volume) :")
for _, row in btc_markets.head(5).iterrows():
    print(f"    [{row['duration_sec']}s] {row['question'][:70]}")

btc_5min = btc_markets[
    (btc_markets["duration_sec"] >= 240) &
    (btc_markets["duration_sec"] <= 480)
].copy()

print(f"\n  Marchés de 4-8 minutes : {len(btc_5min)}")

if len(btc_5min) < 10:
    # Essayer une fenêtre plus large
    btc_5min = btc_markets[
        (btc_markets["duration_sec"] >= 60) &
        (btc_markets["duration_sec"] <= 900)
    ].copy()
    print(f"  Élargissement à 1-15 minutes : {len(btc_5min)}")

print(f"  Volume total des marchés sélectionnés : {btc_5min['volume_usd'].sum():,.0f}$")

# ── Étape 2 : Explorer le schéma de quant.parquet ────────────────────────
print("\n[2/4] Exploration du schéma quant.parquet...")

schema_sample = con.execute(f"""
    SELECT * FROM read_parquet('{QUANT_FILE}') LIMIT 3
""").df()
print(f"  Colonnes disponibles : {list(schema_sample.columns)}")
print(f"  Exemple de données :")
print(schema_sample.head(3).to_string())

# ── Étape 3 : Charger les trades des marchés sélectionnés ────────────────
print("\n[3/4] Chargement des trades Bitcoin...")

# Identifier les colonnes clés selon le schéma
cols = list(schema_sample.columns)
print(f"  Colonnes : {cols}")

# Détecter la colonne identifiant le marché
market_id_col = next((c for c in ["condition_id", "market_id", "market"] if c in cols), cols[0])
# Détecter la colonne de prix
price_col = next((c for c in ["price", "price_yes", "yes_price"] if c in cols), None)
# Détecter la colonne de timestamp
ts_col = next((c for c in ["timestamp", "ts", "created_at", "block_time"] if c in cols), None)
# Détecter la colonne de résolution (outcome)
outcome_col = next((c for c in ["outcome", "resolution", "winning_side"] if c in cols), None)

print(f"  market_id → {market_id_col}")
print(f"  price     → {price_col}")
print(f"  timestamp → {ts_col}")
print(f"  outcome   → {outcome_col}")

if price_col is None or ts_col is None:
    print("  ERREUR : colonnes price/timestamp non trouvées. Inspecter le schéma.")
    sys.exit(1)

# IDs des marchés BTC sélectionnés
btc_ids = btc_5min["condition_id"].dropna().tolist()
ids_sql  = ",".join(f"'{i}'" for i in btc_ids[:2000])  # limite sécurité

trades_query = f"""
SELECT
    t.{market_id_col},
    t.{ts_col}         AS ts,
    t.{price_col}      AS price_yes,
    {f't.{outcome_col}' if outcome_col else 'NULL'} AS outcome_raw
FROM read_parquet('{QUANT_FILE}') t
WHERE t.{market_id_col} IN ({ids_sql})
ORDER BY t.{market_id_col}, t.{ts_col}
"""

print("  Exécution de la requête trades...")
trades = con.execute(trades_query).df()
print(f"  Trades chargés : {len(trades):,}  pour {trades[market_id_col].nunique()} marchés")

if len(trades) == 0:
    print("  ERREUR : aucun trade trouvé. Vérifier le champ market_id dans quant.parquet.")
    sys.exit(1)

# Fusionner avec les métadonnées marchés pour avoir end_date et resolution
trades = trades.merge(
    btc_5min[["condition_id", "end_date_iso", "resolution", "duration_sec"]],
    left_on=market_id_col,
    right_on="condition_id",
    how="inner"
)
print(f"  Après fusion avec métadonnées : {len(trades):,} trades")

# ── Étape 4 : Conversion timestamps et calcul du temps avant résolution ──
print("\n[4/4] Calcul du temps avant résolution...")

# Convertir les timestamps en datetime UTC
trades["ts"] = pd.to_datetime(trades["ts"], utc=True, errors="coerce")
trades["end_dt"] = pd.to_datetime(trades["end_date_iso"], utc=True, errors="coerce")
trades = trades.dropna(subset=["ts", "end_dt"])

# Secondes avant la résolution (valeur positive = avant la fin)
trades["secs_before_end"] = (trades["end_dt"] - trades["ts"]).dt.total_seconds()

# Garde seulement les trades dans les 5 dernières minutes
trades_5min = trades[
    (trades["secs_before_end"] >= 0) &
    (trades["secs_before_end"] <= 300)
].copy()
print(f"  Trades dans les 5 dernières minutes : {len(trades_5min):,}")

# Identifier la résolution finale par marché
# Chercher dans les données ou dans les métadonnées
if outcome_col and trades["outcome_raw"].notna().sum() > 0:
    # Résolution depuis le champ outcome
    resolution_map = (
        trades.dropna(subset=["outcome_raw"])
        .groupby(market_id_col)["outcome_raw"]
        .last()
        .to_dict()
    )
    print(f"  Résolutions trouvées via champ outcome : {len(resolution_map)}")
else:
    # Résolution depuis les métadonnées marchés
    resolution_map = (
        btc_5min.set_index("condition_id")["resolution"]
        .dropna()
        .to_dict()
    )
    print(f"  Résolutions depuis métadonnées marchés : {len(resolution_map)}")

# Normaliser la résolution : 1 = YES a gagné, 0 = NO a gagné
def parse_resolution(val):
    if pd.isna(val):
        return np.nan
    s = str(val).lower().strip()
    if s in ("yes", "1", "1.0", "true", "win"):
        return 1.0
    if s in ("no", "0", "0.0", "false", "lose"):
        return 0.0
    try:
        v = float(s)
        if v >= 0.99:
            return 1.0
        if v <= 0.01:
            return 0.0
    except ValueError:
        pass
    return np.nan

res_yes_won = {k: parse_resolution(v) for k, v in resolution_map.items()}

# Ajouter la résolution à chaque trade
trades_5min["yes_won"] = trades_5min[market_id_col].map(res_yes_won)
trades_5min = trades_5min.dropna(subset=["yes_won"])

n_markets_usable = trades_5min[market_id_col].nunique()
print(f"  Marchés utilisables (résolution connue + trades 5min) : {n_markets_usable}")

if n_markets_usable < 20:
    print("  ⚠  Peu de marchés utilisables. Résultats statistiquement limités.")

# ── Analyse par fenêtre temporelle ───────────────────────────────────────
print("\nAnalyse en cours...")

results = []

for window_sec in ALL_WINDOWS:
    # Pour chaque marché : prendre le dernier prix connu dans la fenêtre
    # i.e. le trade le plus récent dont secs_before_end >= window_sec
    window_trades = trades_5min[
        trades_5min["secs_before_end"] >= window_sec
    ].copy()

    if len(window_trades) == 0:
        continue

    # Dernier trade avant (ou à) la fenêtre pour chaque marché
    last_price = (
        window_trades
        .sort_values("ts")
        .groupby(market_id_col)
        .last()
        [["price_yes", "yes_won"]]
        .reset_index()
    )

    if len(last_price) < 5:
        continue

    # Prix YES observé à ce moment
    p_yes = last_price["price_yes"].values
    yes_won = last_price["yes_won"].values

    # Identifier upsider et underdog
    # upsider = YES si prix_yes > 0.5, else NO
    yes_is_upsider = p_yes > 0.5

    # ── Win rate : upsider ───────────────────────────────────────
    # Upsider gagne si :
    #   - YES est upsider ET YES gagne  (yes_won=1)
    #   - NO  est upsider ET NO  gagne  (yes_won=0)
    upsider_wins = np.where(yes_is_upsider, yes_won == 1, yes_won == 0)
    underdog_wins = ~upsider_wins

    wr_upsider  = upsider_wins.mean()
    wr_underdog = underdog_wins.mean()

    # ── ROI : upsider ────────────────────────────────────────────
    # Si on bet $1 sur l'upsider au prix P_upsider :
    #   P_upsider = prix_yes si YES est upsider, sinon (1 - prix_yes)
    p_upsider  = np.where(yes_is_upsider, p_yes, 1.0 - p_yes)
    p_underdog = 1.0 - p_upsider

    # Éviter division par zéro
    p_upsider  = np.clip(p_upsider,  0.01, 0.99)
    p_underdog = np.clip(p_underdog, 0.01, 0.99)

    # ROI = win_rate * gain_net_si_victoire - (1-win_rate) * mise
    # = win_rate * (1/P - 1) - (1-win_rate)
    roi_upsider_per_trade = np.where(
        upsider_wins,
        (1.0 / p_upsider - 1.0),   # gain net
        -1.0                         # perte totale de la mise
    )
    roi_underdog_per_trade = np.where(
        underdog_wins,
        (1.0 / p_underdog - 1.0),
        -1.0
    )

    roi_upsider  = roi_upsider_per_trade.mean()
    roi_underdog = roi_underdog_per_trade.mean()

    # Prix moyen observé
    p_yes_mean = p_yes.mean()
    p_yes_std  = p_yes.std()

    results.append({
        "secs_before":     window_sec,
        "n_markets":       len(last_price),
        "p_yes_mean":      p_yes_mean,
        "p_yes_std":       p_yes_std,
        "wr_upsider":      wr_upsider,
        "wr_underdog":     wr_underdog,
        "roi_upsider":     roi_upsider,
        "roi_underdog":    roi_underdog,
        "roi_upsider_pct": roi_upsider * 100,
        "roi_underdog_pct":roi_underdog * 100,
    })

df = pd.DataFrame(results).sort_values("secs_before", ascending=False)

# ── Affichage du tableau bilan ────────────────────────────────────────────
print("\n" + "=" * 100)
print("  BILAN — HEDGE SUR MARCHÉS BITCOIN UP/DOWN 5 MINUTES")
print("=" * 100)
print(f"\n  Marchés analysés : {n_markets_usable}")
print(f"  Fenêtres temporelles : {len(df)} points\n")

# Entête du tableau
header = f"  {'Temps avant':>12}  {'N marchés':>9}  {'P_yes moy':>9}  "
header += f"{'WR upsider':>10}  {'ROI upsider':>11}  "
header += f"{'WR underdog':>11}  {'ROI underdog':>12}"
print(header)
print("  " + "-" * 96)

for _, row in df.iterrows():
    # Highlight les lignes avec ROI positif
    upsider_positive  = "✓" if row["roi_upsider"]  > 0 else " "
    underdog_positive = "✓" if row["roi_underdog"] > 0 else " "

    secs = int(row["secs_before"])
    if secs >= 60:
        time_label = f"{secs//60}m{secs%60:02d}s"
    else:
        time_label = f"    {secs}s"

    print(
        f"  {time_label:>12}  "
        f"{int(row['n_markets']):>9}  "
        f"{row['p_yes_mean']:>8.1%}  "
        f"  {row['wr_upsider']:>8.1%}  "
        f"  {upsider_positive}{row['roi_upsider_pct']:>9.1f}%  "
        f"  {row['wr_underdog']:>9.1%}  "
        f"  {underdog_positive}{row['roi_underdog_pct']:>9.1f}%"
    )

print()

# ── Résumé ────────────────────────────────────────────────────────────────
print("=" * 100)
print("  INTERPRÉTATION")
print("=" * 100)

# Trouver les meilleures opportunités
best_upsider  = df.loc[df["roi_upsider"].idxmax()]
best_underdog = df.loc[df["roi_underdog"].idxmax()]

def secs_to_label(s):
    s = int(s)
    return f"{s//60}m{s%60:02d}s" if s >= 60 else f"{s}s"

print(f"\n  Meilleur ROI upsider  : {best_upsider['roi_upsider_pct']:+.1f}% "
      f"à {secs_to_label(best_upsider['secs_before'])} avant résolution "
      f"(WR = {best_upsider['wr_upsider']:.1%}, N = {int(best_upsider['n_markets'])})")

print(f"  Meilleur ROI underdog : {best_underdog['roi_underdog_pct']:+.1f}% "
      f"à {secs_to_label(best_underdog['secs_before'])} avant résolution "
      f"(WR = {best_underdog['wr_underdog']:.1%}, N = {int(best_underdog['n_markets'])})")

# Fenêtres avec ROI positif > 2%
upsider_positive_windows  = df[df["roi_upsider"]  > 0.02]
underdog_positive_windows = df[df["roi_underdog"] > 0.02]

print(f"\n  Fenêtres avec ROI upsider > 2%  : {len(upsider_positive_windows)}")
if len(upsider_positive_windows) > 0:
    for _, r in upsider_positive_windows.iterrows():
        print(f"    → {secs_to_label(r['secs_before']):<8}  ROI={r['roi_upsider_pct']:+.1f}%  "
              f"WR={r['wr_upsider']:.1%}  N={int(r['n_markets'])}")

print(f"\n  Fenêtres avec ROI underdog > 2% : {len(underdog_positive_windows)}")
if len(underdog_positive_windows) > 0:
    for _, r in underdog_positive_windows.iterrows():
        print(f"    → {secs_to_label(r['secs_before']):<8}  ROI={r['roi_underdog_pct']:+.1f}%  "
              f"WR={r['wr_underdog']:.1%}  N={int(r['n_markets'])}")

# ── Sauvegarde CSV ────────────────────────────────────────────────────────
csv_path = OUTPUT_DIR / "btc_5min_hedge_results.csv"
df.to_csv(csv_path, index=False)
print(f"\n  Résultats CSV : {csv_path}")

# ── Graphiques ────────────────────────────────────────────────────────────
fig, axes = plt.subplots(2, 2, figsize=(16, 10))
fig.suptitle("Marchés Bitcoin up/down 5 minutes — Analyse de hedge", fontsize=14, fontweight="bold")

x = df["secs_before"].values
x_labels = [secs_to_label(s) for s in x]

# 1. Win rate
ax = axes[0, 0]
ax.plot(x, df["wr_upsider"] * 100,  "b-o", markersize=4, label="Upsider (majoritaire)")
ax.plot(x, df["wr_underdog"] * 100, "r-o", markersize=4, label="Underdog (minoritaire)")
ax.axhline(50, color="gray", linestyle="--", alpha=0.5, label="50% (pile-face)")
ax.set_xlabel("Secondes avant résolution")
ax.set_ylabel("Win rate (%)")
ax.set_title("Win rate selon le temps avant résolution")
ax.legend()
ax.set_xlim(max(x), 0)
ax.grid(True, alpha=0.3)
ax.yaxis.set_major_formatter(mtick.PercentFormatter())

# 2. ROI
ax = axes[0, 1]
ax.plot(x, df["roi_upsider_pct"],  "b-o", markersize=4, label="Upsider")
ax.plot(x, df["roi_underdog_pct"], "r-o", markersize=4, label="Underdog")
ax.axhline(0, color="black", linewidth=1.5, label="Break-even")
ax.fill_between(x, df["roi_upsider_pct"], 0,
                where=(df["roi_upsider_pct"] > 0), alpha=0.15, color="blue")
ax.fill_between(x, df["roi_underdog_pct"], 0,
                where=(df["roi_underdog_pct"] > 0), alpha=0.15, color="red")
ax.set_xlabel("Secondes avant résolution")
ax.set_ylabel("ROI moyen (%)")
ax.set_title("ROI moyen selon le temps avant résolution")
ax.legend()
ax.set_xlim(max(x), 0)
ax.grid(True, alpha=0.3)
ax.yaxis.set_major_formatter(mtick.PercentFormatter())

# 3. Prix YES moyen observé dans le temps
ax = axes[1, 0]
ax.plot(x, df["p_yes_mean"] * 100, "g-o", markersize=4)
ax.fill_between(x,
                (df["p_yes_mean"] - df["p_yes_std"]) * 100,
                (df["p_yes_mean"] + df["p_yes_std"]) * 100,
                alpha=0.15, color="green", label="±1 écart-type")
ax.axhline(50, color="gray", linestyle="--", alpha=0.5)
ax.set_xlabel("Secondes avant résolution")
ax.set_ylabel("Prix YES moyen (%)")
ax.set_title("Évolution du prix YES dans les 5 dernières minutes")
ax.legend()
ax.set_xlim(max(x), 0)
ax.grid(True, alpha=0.3)
ax.yaxis.set_major_formatter(mtick.PercentFormatter())

# 4. Nombre de marchés avec données
ax = axes[1, 1]
ax.bar(range(len(x)), df["n_markets"].values, color="steelblue", alpha=0.7)
ax.set_xticks(range(len(x)))
ax.set_xticklabels(x_labels, rotation=90, fontsize=7)
ax.set_xlabel("Fenêtre temporelle")
ax.set_ylabel("Nombre de marchés")
ax.set_title("Nombre de marchés avec données par fenêtre")
ax.grid(True, alpha=0.3, axis="y")

plt.tight_layout()
png_path = OUTPUT_DIR / "btc_5min_hedge_chart.png"
plt.savefig(png_path, dpi=150, bbox_inches="tight")
print(f"  Graphique    : {png_path}")
plt.close()

print("\nAnalyse terminée.")
