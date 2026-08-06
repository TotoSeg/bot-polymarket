import pandas as pd, duckdb
conn = duckdb.connect()

r = conn.execute("""
    SELECT start_ts, end_ts, question
    FROM read_parquet('data/updown/markets_5m.parquet')
    WHERE resolution IN (0,1) LIMIT 3
""").df()
print("start_ts marches:")
print(r.to_string(index=False))

df = pd.read_parquet("data/updown/btc_5m_candles.parquet")
df["open_ts"] = df["open_time"].astype("int64") // 10**9
print("\nopen_ts bougies (3 premiers):")
print(df[["open_time","open_ts"]].head(3).to_string(index=False))

s = int(r["start_ts"].iloc[0])
c = int(df["open_ts"].iloc[0])
print(f"\nstart_ts[0] = {s}  ({pd.to_datetime(s, unit='s', utc=True)})")
print(f"open_ts[0]  = {c}  ({pd.to_datetime(c, unit='s', utc=True)})")
print(f"start_ts % 300 = {s % 300}  (doit etre 0 si aligne sur 5min)")
print(f"open_ts  % 300 = {c % 300}  (doit etre 0 si aligne sur 5min)")

# Chercher si start_ts[0] - 300 existe dans open_ts
prev = s - 300
match = df[df["open_ts"] == prev]
print(f"\nRecherche prev_candle (start_ts - 300 = {prev}) : {len(match)} match(es)")
if match.empty:
    # chercher le plus proche
    diffs = (df["open_ts"] - prev).abs()
    closest = df.loc[diffs.idxmin()]
    print(f"Plus proche : open_ts={closest['open_ts']}  (diff={abs(closest['open_ts']-prev)}s)")
