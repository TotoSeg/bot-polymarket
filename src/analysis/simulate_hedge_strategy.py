"""
Simulation strategie hedge asymetrique sur marches 5m
======================================================
Entree : YES a ~50c, $5
Regle hedge : si YES > 80% a un moment, acheter NO a (prix courant) pour 30% de la mise
Sortie : tenir jusqu a resolution

Calcul sur tous les marches resolus avec ticks disponibles.
"""
import sys, duckdb
import pandas as pd
import numpy as np

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

conn = duckdb.connect()
FEE = 0.02

# Charger ticks + marches resolus
print("Chargement des donnees...")
ticks = conn.execute("""
    SELECT t.market_id, t.outcome, t.price, t.timestamp_ms, t.size_usdc,
           m.resolution, m.end_ts,
           (m.end_ts * 1000 - t.timestamp_ms) / 1000.0 AS secs_remaining
    FROM read_parquet('data/updown/ticks_5m.parquet') t
    INNER JOIN read_parquet('data/updown/markets_5m.parquet') m
        ON t.market_id = m.market_id
    WHERE t.timestamp_ms > 0
      AND t.price BETWEEN 0.01 AND 0.99
      AND m.resolution IN (0, 1)
      AND (m.end_ts * 1000 - t.timestamp_ms) / 1000.0 BETWEEN 0 AND 300
    ORDER BY t.market_id, t.timestamp_ms
""").df()

print(f"  {len(ticks):,} ticks sur {ticks['market_id'].nunique():,} marches")

# Parametres de la strategie
BET_YES   = 5.0     # mise initiale YES
HEDGE_PCT = 0.30    # 30% de la mise initiale sur le hedge NO
HEDGE_TRIGGER = 0.80  # declencher le hedge quand YES > 80%

results = []

for market_id, group in ticks.groupby("market_id"):
    group = group.sort_values("timestamp_ms").reset_index(drop=True)
    resolution = group["resolution"].iloc[0]

    # Prix d ouverture du marche
    first_up   = group[group["outcome"] == "Up"].head(1)
    first_down = group[group["outcome"] == "Down"].head(1)
    if first_up.empty or first_down.empty:
        continue

    open_yes = first_up["price"].iloc[0]

    # On entre uniquement si le marche ouvre entre 40-60c
    if not (0.40 <= open_yes <= 0.60):
        continue

    yes_tokens = BET_YES / open_yes
    hedge_placed = False
    hedge_cost = 0.0
    hedge_no_tokens = 0.0

    # Simuler l evolution du prix dans la fenetre
    for _, row in group.iterrows():
        if row["outcome"] != "Up":
            continue
        yes_price = row["price"]
        secs_rem  = row["secs_remaining"]

        # Regle hedge : YES > 80% et pas encore hedge et il reste > 30s
        if not hedge_placed and yes_price >= HEDGE_TRIGGER and secs_rem > 30:
            no_price = 1.0 - yes_price
            hedge_cost = BET_YES * HEDGE_PCT
            hedge_no_tokens = hedge_cost / no_price
            hedge_placed = True
            hedge_yes_price_at_trigger = yes_price

    # Calcul P&L a la resolution
    if resolution == 1:  # YES gagne
        pnl_yes = yes_tokens * (1 - open_yes) * (1 - FEE)
        pnl_no  = -hedge_cost if hedge_placed else 0
    else:                 # NO gagne
        pnl_yes = -BET_YES
        pnl_no  = hedge_no_tokens * (1 - (1.0 - (1-open_yes))) * (1 - FEE) if hedge_placed else 0
        # Correction : NO token paie $1 si NO gagne
        pnl_no  = hedge_no_tokens * (1 - (hedge_cost / hedge_no_tokens)) * (1 - FEE) if hedge_placed else 0
        # Plus simple : gain NO = tokens * (1 - price_no_bought) * 0.98
        if hedge_placed:
            no_price_bought = hedge_cost / hedge_no_tokens
            pnl_no = hedge_no_tokens * (1 - no_price_bought) * (1 - FEE)

    total_pnl = pnl_yes + pnl_no
    total_invested = BET_YES + hedge_cost

    results.append({
        "market_id":   market_id,
        "resolution":  resolution,
        "open_yes":    open_yes,
        "hedge":       hedge_placed,
        "hedge_cost":  hedge_cost,
        "pnl_yes":     round(pnl_yes, 4),
        "pnl_no":      round(pnl_no, 4),
        "total_pnl":   round(total_pnl, 4),
        "total_inv":   round(total_invested, 4),
    })

df = pd.DataFrame(results)
print(f"\n  Marches simules : {len(df):,}")
print(f"  Dont avec hedge declenche : {df['hedge'].sum():,} ({df['hedge'].mean()*100:.1f}%)")

print("\n" + "=" * 55)
print("RESULTATS STRATEGIE HEDGE ASYMETRIQUE")
print("=" * 55)

# Global
total_pnl = df["total_pnl"].sum()
total_inv  = df["total_inv"].sum()
wins = (df["total_pnl"] > 0).sum()
roi = total_pnl / total_inv * 100

print(f"\n  Nb trades       : {len(df):,}")
print(f"  Capital investi : ${total_inv:,.2f}")
print(f"  P&L total       : ${total_pnl:+,.2f}")
print(f"  ROI             : {roi:+.2f}%")
print(f"  Win rate        : {wins/len(df)*100:.1f}%")
print(f"  EV par trade    : ${total_pnl/len(df):+.4f}")

# Avec vs sans hedge
print("\n--- Comparaison hedge vs no-hedge ---")
with_hedge    = df[df["hedge"]]
without_hedge = df[~df["hedge"]]

for label, sub in [("Avec hedge   ", with_hedge), ("Sans hedge   ", without_hedge)]:
    if len(sub) == 0: continue
    roi_s = sub["total_pnl"].sum() / sub["total_inv"].sum() * 100
    ev_s  = sub["total_pnl"].mean()
    print(f"  {label} | n={len(sub):>4} | ROI={roi_s:+.2f}% | EV/trade=${ev_s:+.4f}")

# Baseline : achat YES sans hedge
print("\n--- Baseline : YES seul sans hedge ---")
baseline_pnl = df.apply(lambda r: r["pnl_yes"], axis=1).sum()
baseline_roi = baseline_pnl / (BET_YES * len(df)) * 100
print(f"  ROI baseline  : {baseline_roi:+.2f}%")
print(f"  EV/trade base : ${baseline_pnl/len(df):+.4f}")

# Breakdown par resolution quand hedge declenche
print("\n--- Quand hedge declenche : P&L par scenario ---")
h = df[df["hedge"]]
if len(h) > 0:
    for res, lbl in [(1,"YES gagne"), (0,"NO gagne (mean-rev)")]:
        sub = h[h["resolution"]==res]
        if len(sub) == 0: continue
        print(f"  {lbl} | n={len(sub):>3} | avg P&L=${sub['total_pnl'].mean():+.3f} | "
              f"avg pnl_yes=${sub['pnl_yes'].mean():+.3f} | avg pnl_no=${sub['pnl_no'].mean():+.3f}")
