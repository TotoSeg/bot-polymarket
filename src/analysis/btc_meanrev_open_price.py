"""
Mean-reversion deja pricee a l'ouverture du marche suivant ?
=============================================================
Pour chaque paire de fenetres consecutives :
  - Direction bougie N (up/down) depuis btc_5m_candles.parquet
  - Prix d'ouverture du marche N+1 dans ticks_5m.parquet
    (premier tick du marche = proxy du prix a l'ouverture)

Si le biais mean-reversion est deja prise en compte par les market makers,
on devrait voir : apres bougie UP, 'Down' s'ouvre > 50c (deja sur-price).
Si non, 'Down' s'ouvre a ~50c et il y a de l'edge.
"""

import sys
import duckdb
import pandas as pd
import numpy as np

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

conn = duckdb.connect()

print("=" * 60)
print("MEAN-REVERSION : DEJA PRICEE A L'OUVERTURE ?")
print("=" * 60)

# Prix d'ouverture = premier tick de chaque marche (timestamp_ms min)
# Direction bougie precedente = depuis btc_5m_candles (open_time = start_ts du marche)
# On joint via start_ts : chaque marche 5m correspond a une bougie 5m BTC

print("\nChargement des donnees...")

# Prix d'ouverture par marche (premier tick)
df_open = conn.execute("""
    SELECT
        t.market_id,
        t.crypto,
        t.outcome,
        MIN(t.timestamp_ms) AS first_tick_ms,
        FIRST(t.price ORDER BY t.timestamp_ms ASC) AS open_price
    FROM read_parquet('data/updown/ticks_5m.parquet') t
    INNER JOIN read_parquet('data/updown/markets_5m.parquet') m
        ON t.market_id = m.market_id
    WHERE t.timestamp_ms > 0
      AND m.resolution IN (0, 1)
    GROUP BY t.market_id, t.crypto, t.outcome
""").df()

print(f"  Prix ouverture : {len(df_open):,} lignes ({df_open['market_id'].nunique():,} marches)")

# Metadata marches avec start_ts et resolution
df_markets = conn.execute("""
    SELECT market_id, start_ts, end_ts, resolution, crypto
    FROM read_parquet('data/updown/markets_5m.parquet')
    WHERE resolution IN (0, 1)
""").df()

# Bougies BTC 5m (open_time en UTC, on convertit en epoch secondes)
df_candles = pd.read_parquet("data/updown/btc_5m_candles.parquet")
df_candles["open_ts"] = df_candles["open_time"].astype("int64") // 10**9
df_candles["dir"] = (df_candles["close"] > df_candles["open"]).astype(int)
df_candles["ret_pct"] = (df_candles["close"] - df_candles["open"]) / df_candles["open"] * 100

print(f"  Bougies BTC : {len(df_candles):,}")

# Jointure : marche -> bougie precedente via start_ts
# start_ts du marche N+1 = end_ts du marche N = open_time de la bougie N
df_mkt = df_markets.copy()
df_mkt["prev_candle_ts"] = df_mkt["start_ts"]  # debut du marche = debut de la bougie contemporaine

# Associer la bougie precedente (celle qui se terminait au start_ts du marche)
# La bougie qui finit a T = ouverte a T-300s
df_mkt["prev_open_ts"] = df_mkt["start_ts"] - 300

candle_lookup = df_candles.set_index("open_ts")[["dir", "ret_pct"]]
df_mkt["prev_dir"] = df_mkt["prev_open_ts"].map(candle_lookup["dir"])
df_mkt["prev_ret"]  = df_mkt["prev_open_ts"].map(candle_lookup["ret_pct"])

df_mkt = df_mkt.dropna(subset=["prev_dir"])
print(f"  Marches avec bougie precedente connue : {len(df_mkt):,}")

# Jointure avec prix d'ouverture
# On prend seulement le Down token (outcome='Down') pour voir si il est sur/sous-price
df_open_down = df_open[df_open["outcome"] == "Down"][["market_id", "open_price"]].copy()
df_open_up   = df_open[df_open["outcome"] == "Up"][["market_id", "open_price"]].rename(
    columns={"open_price": "open_price_up"})

df_joined = df_mkt.merge(df_open_down, on="market_id", how="inner")
df_joined = df_joined.merge(df_open_up, on="market_id", how="left")
df_joined["prev_dir"] = df_joined["prev_dir"].astype(int)

print(f"  Paires analysables : {len(df_joined):,}")

# ── Analyse principale ───────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("1. PRIX D'OUVERTURE 'DOWN' SELON DIRECTION BOUGIE PRECEDENTE")
print("=" * 60)
print("(Si mean-reversion pricee : apres UP, Down s'ouvre > 50c)\n")

g = df_joined.groupby("prev_dir")["open_price"].agg(["mean", "median", "std", "count"])
g.index = ["prev=Down", "prev=Up"]
g.columns = ["avg_open_down", "median", "std", "n"]
g["avg_open_down"] = (g["avg_open_down"] * 100).round(3)
g["median"] = (g["median"] * 100).round(3)
print(g.to_string())

avg_after_up   = df_joined[df_joined["prev_dir"]==1]["open_price"].mean()
avg_after_down = df_joined[df_joined["prev_dir"]==0]["open_price"].mean()
print(f"\n  Prix moyen 'Down' apres bougie UP   : {avg_after_up*100:.3f}c")
print(f"  Prix moyen 'Down' apres bougie DOWN : {avg_after_down*100:.3f}c")
print(f"  Difference                          : {(avg_after_up-avg_after_down)*100:+.3f}pp")

# ── Par magnitude ────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("2. PRIX OUVERTURE 'DOWN' SELON MAGNITUDE BOUGIE PRECEDENTE")
print("=" * 60)
print(f"{'Magnitude':>14} | {'Apres UP (Down price)':>22} | {'Apres DOWN (Down price)':>24} | {'diff':>8} | edge?")

bins   = [0, 0.05, 0.15, 0.30, 0.60, np.inf]
labels = ["<0.05%", "0.05-0.15%", "0.15-0.30%", "0.30-0.60%", ">0.60%"]
df_joined["mag"] = pd.cut(df_joined["prev_ret"].abs(), bins=bins, labels=labels)

for lbl in labels:
    sub = df_joined[df_joined["mag"] == lbl]
    if len(sub) < 50:
        continue
    pu = sub[sub["prev_dir"]==1]["open_price"].mean()
    pd_ = sub[sub["prev_dir"]==0]["open_price"].mean()
    diff = (pu - pd_) * 100
    # Edge = prix Down apres UP vs win_rate attendu
    # WR attendu apres UP (mean-rev) selon candle analysis :
    # <0.05%: ~50%, 0.05-0.15%: ~51.7%, 0.15-0.30%: ~53.3%, 0.30-0.60%: ~54.2%, >0.60%: ~54.2%
    wr_expected = {"<0.05%": 0.497, "0.05-0.15%": 0.517, "0.15-0.30%": 0.533,
                   "0.30-0.60%": 0.542, ">0.60%": 0.542}.get(lbl, 0.5)
    ev = wr_expected * (1-pu)/pu * 0.98 - (1-wr_expected) if pu > 0 else 0
    edge = f"EV={ev*100:+.1f}%" if abs(ev) > 0.005 else "~neutre"
    print(f"{lbl:>14} | {pu*100:>21.3f}c | {pd_*100:>23.3f}c | {diff:>+7.2f}pp | {edge}")

print("\n=> Si 'Down' apres gros UP s'ouvre proche de 50c → edge non price → strategie viable")
print("=> Si 'Down' apres gros UP s'ouvre deja a 54c+ → edge deja integre → pas d'edge restant")
