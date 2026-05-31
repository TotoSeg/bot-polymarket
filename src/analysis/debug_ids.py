import sys, duckdb
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
conn = duckdb.connect()

print("=== IDs dans ticks_5m (10 premiers) ===")
print(conn.execute("""
    SELECT market_id, typeof(market_id) as type
    FROM read_parquet('data/updown/ticks_5m.parquet')
    LIMIT 5
""").df().to_string(index=False))

print("\n=== IDs dans resolutions_gamma (10 premiers) ===")
print(conn.execute("""
    SELECT market_id, typeof(market_id) as type, asset, resolution
    FROM read_parquet('data/updown/resolutions_gamma.parquet')
    LIMIT 5
""").df().to_string(index=False))

print("\n=== IDs dans markets_5m (10 premiers) ===")
print(conn.execute("""
    SELECT market_id, typeof(market_id) as type, question
    FROM read_parquet('data/updown/markets_5m.parquet')
    LIMIT 5
""").df().to_string(index=False))

print("\n=== Plage des IDs ticks ===")
print(conn.execute("""
    SELECT MIN(market_id) as min_id, MAX(market_id) as max_id,
           COUNT(DISTINCT market_id) as nb_distinct
    FROM read_parquet('data/updown/ticks_5m.parquet')
""").df().to_string(index=False))

print("\n=== Plage des IDs resolutions ===")
print(conn.execute("""
    SELECT MIN(CAST(market_id AS BIGINT)) as min_id,
           MAX(CAST(market_id AS BIGINT)) as max_id,
           COUNT(DISTINCT market_id) as nb_distinct
    FROM read_parquet('data/updown/resolutions_gamma.parquet')
""").df().to_string(index=False))
