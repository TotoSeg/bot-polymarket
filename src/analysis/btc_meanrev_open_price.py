import sys, pandas as pd, numpy as np, duckdb
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

conn = duckdb.connect()

print("=" * 60)
print("MEAN-REVERSION : DEJA PRICEE A L OUVERTURE ?")
print("=" * 60)

# Prix d'ouverture = premier tick par marche
df_open = conn.execute("""
    SELECT t.market_id, t.outcome,
        FIRST(t.price ORDER BY t.timestamp_ms ASC) AS open_price
    FROM read_parquet('data/updown/ticks_5m.parquet') t
    INNER JOIN read_parquet('data/updown/markets_5m.parquet') m
        ON t.market_id = m.market_id
    WHERE t.timestamp_ms > 0 AND m.resolution IN (0,1)
    GROUP BY t.market_id, t.outcome
""").df()

df_mkts = conn.execute("""
    SELECT market_id, end_ts, resolution
    FROM read_parquet('data/updown/markets_5m.parquet')
    WHERE resolution IN (0,1)
""").df()

# Candles BTC : open_time stockee en ms dans parquet -> convertir en secondes
df_c = pd.read_parquet("data/updown/btc_5m_candles.parquet")
# open_time est datetime64[ms, UTC] -> astype int64 donne des ms depuis epoch
df_c["open_ts"] = df_c["open_time"].astype("int64") // 1000  # ms -> secondes
df_c["dir"] = (df_c["close"] > df_c["open"]).astype(int)
df_c["ret_pct"] = (df_c["close"] - df_c["open"]) / df_c["open"] * 100
df_c = df_c.drop_duplicates(subset=["open_ts"], keep="last")
candle_idx = df_c.set_index("open_ts")

# Fenetre du marche : end_ts est aligne 5min. Bougie precedente : ouverte a end_ts - 600
# (la fenetre du marche est end_ts-300 -> end_ts, donc bougie precedente = end_ts-600)
df_mkts["prev_candle_ts"] = df_mkts["end_ts"].astype(int) - 600
df_mkts["prev_dir"] = df_mkts["prev_candle_ts"].map(candle_idx["dir"])
df_mkts["prev_ret"]  = df_mkts["prev_candle_ts"].map(candle_idx["ret_pct"])
df_mkts = df_mkts.dropna(subset=["prev_dir"])
df_mkts["prev_dir"] = df_mkts["prev_dir"].astype(int)

print(f"Marches avec bougie precedente connue : {len(df_mkts):,}")

# Jointure avec prix ouverture 'Down'
df_down = df_open[df_open["outcome"] == "Down"][["market_id","open_price"]]
df_up   = df_open[df_open["outcome"] == "Up"][["market_id","open_price"]].rename(
    columns={"open_price":"open_price_up"})
df = df_mkts.merge(df_down, on="market_id").merge(df_up, on="market_id", how="left")

print(f"Paires analysables : {len(df):,}")
if len(df) == 0:
    # debug : afficher quelques valeurs
    print("DEBUG end_ts sample:", df_mkts["end_ts"].head(3).tolist())
    print("DEBUG prev_candle_ts sample:", df_mkts["prev_candle_ts"].head(3).tolist())
    print("DEBUG candle open_ts sample:", df_c["open_ts"].head(3).tolist())
    print("DEBUG end_ts % 300 sample:", (df_mkts["end_ts"] % 300).head(3).tolist())
    print("DEBUG open_ts % 300 sample:", (df_c["open_ts"] % 300).head(3).tolist())
    exit()

print()
print("=" * 60)
print("1. PRIX OUVERTURE 'DOWN' SELON DIRECTION BOUGIE PRECEDENTE")
print("=" * 60)
print("(Si mean-rev pricee : apres UP, Down s ouvre > 50c)\n")

for d, lbl in [(1,"prev=Up"), (0,"prev=Down")]:
    sub = df[df["prev_dir"]==d]["open_price"]
    print(f"  {lbl} | n={len(sub):,} | avg={sub.mean()*100:.3f}c | median={sub.median()*100:.3f}c")

diff = (df[df["prev_dir"]==1]["open_price"].mean() - df[df["prev_dir"]==0]["open_price"].mean()) * 100
print(f"\n  Difference apres UP vs DOWN : {diff:+.3f}pp")
print("  => Si > 0 : le marche anticipe deja la mean-reversion (Down plus cher apres UP)")
print("  => Si ~ 0 : edge non price -> strategie viable")

print()
print("=" * 60)
print("2. PAR MAGNITUDE BOUGIE PRECEDENTE")
print("=" * 60)

bins   = [0, 0.05, 0.15, 0.30, 0.60, np.inf]
labels = ["<0.05%","0.05-0.15%","0.15-0.30%","0.30-0.60%",">0.60%"]
wr_exp = {"<0.05%":0.497,"0.05-0.15%":0.517,"0.15-0.30%":0.533,"0.30-0.60%":0.542,">0.60%":0.542}
df["mag"] = pd.cut(df["prev_ret"].abs(), bins=bins, labels=labels)

print(f"{'Magnitude':>14} | {'Down apres UP':>14} | {'Down apres DOWN':>16} | {'diff':>8} | EV si achat Down apres UP")
for lbl in labels:
    sub = df[df["mag"]==lbl]
    if len(sub) < 20: continue
    pu  = sub[sub["prev_dir"]==1]["open_price"].mean()
    pd_ = sub[sub["prev_dir"]==0]["open_price"].mean()
    if np.isnan(pu) or np.isnan(pd_): continue
    wr  = wr_exp[lbl]
    ev  = wr*(1-pu)/pu*0.98 - (1-wr) if pu>0 else 0
    print(f"{lbl:>14} | {pu*100:>13.3f}c | {pd_*100:>15.3f}c | {(pu-pd_)*100:>+7.3f}pp | EV={ev*100:+.2f}%")
