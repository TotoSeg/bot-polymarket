import sys, duckdb
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

conn = duckdb.connect()
print("=" * 65)
print("ANALYSE SY - MARCHES UP/DOWN 5 MINUTES")
print("=" * 65)

print("\n--- Verification jointure ---")
print(conn.execute("""
    SELECT COUNT(DISTINCT t.market_id) AS ticks_markets,
           COUNT(DISTINCT CASE WHEN m.resolution IN (0,1) THEN t.market_id END) AS overlap
    FROM read_parquet('data/updown/ticks_5m.parquet') t
    LEFT JOIN read_parquet('data/updown/markets_5m.parquet') m ON t.market_id = m.market_id
    WHERE t.timestamp_ms > 0
""").df().to_string(index=False))

print("\n--- Win rate par prix x temps restant (dernieres 90s) ---\n")
df = conn.execute("""
    WITH base AS (
        SELECT t.market_id, t.crypto, t.outcome, t.price,
            m.end_ts, m.resolution,
            (m.end_ts * 1000 - t.timestamp_ms) / 1000.0 AS secs_remaining,
            CASE WHEN t.outcome = 'Up'   AND m.resolution = 1 THEN 1
                 WHEN t.outcome = 'Down' AND m.resolution = 0 THEN 1
                 ELSE 0 END AS is_win
        FROM read_parquet('data/updown/ticks_5m.parquet') t
        INNER JOIN read_parquet('data/updown/markets_5m.parquet') m ON t.market_id = m.market_id
        WHERE t.timestamp_ms > 0
          AND t.price BETWEEN 0.01 AND 0.99
          AND m.resolution IN (0, 1)
          AND (m.end_ts * 1000 - t.timestamp_ms) / 1000.0 BETWEEN 0 AND 90
    ),
    buckets AS (
        SELECT market_id,
            CASE WHEN price >= 0.98 THEN '98-100%'
                 WHEN price >= 0.95 THEN '95-98%'
                 WHEN price >= 0.92 THEN '92-95%'
                 WHEN price >= 0.88 THEN '88-92%'
                 WHEN price >= 0.80 THEN '80-88%'
                 ELSE '<80%' END AS price_bucket,
            CASE WHEN secs_remaining <= 15 THEN '00-15s'
                 WHEN secs_remaining <= 30 THEN '15-30s'
                 WHEN secs_remaining <= 60 THEN '30-60s'
                 ELSE '60-90s' END AS time_bucket,
            is_win, price
        FROM base
    )
    SELECT price_bucket, time_bucket,
        COUNT(*) AS nb_trades,
        COUNT(DISTINCT market_id) AS nb_marches,
        ROUND(100.0 * SUM(is_win) / COUNT(*), 2) AS win_rate_pct,
        ROUND(AVG(price), 4) AS avg_price,
        ROUND(SUM(is_win)::FLOAT/COUNT(*)*(1-AVG(price))/AVG(price)*0.98
              -(1-SUM(is_win)::FLOAT/COUNT(*)), 4) AS ev_net
    FROM buckets
    GROUP BY price_bucket, time_bucket
    ORDER BY price_bucket DESC, time_bucket
""").df()
print(df.to_string(index=False))

print("\n--- Combinaisons EV > 0 ---")
pos = df[df["ev_net"] > 0].sort_values("ev_net", ascending=False)
print(pos.to_string(index=False) if not pos.empty else "  Aucune.")

print("\n--- Par asset (prix >= 92%, dernieres 90s) ---\n")
print(conn.execute("""
    WITH base AS (
        SELECT t.crypto, t.market_id, t.price, m.resolution,
            CASE WHEN t.outcome = 'Up'   AND m.resolution = 1 THEN 1
                 WHEN t.outcome = 'Down' AND m.resolution = 0 THEN 1
                 ELSE 0 END AS is_win
        FROM read_parquet('data/updown/ticks_5m.parquet') t
        INNER JOIN read_parquet('data/updown/markets_5m.parquet') m ON t.market_id = m.market_id
        WHERE t.timestamp_ms > 0
          AND t.price >= 0.92
          AND m.resolution IN (0, 1)
          AND (m.end_ts * 1000 - t.timestamp_ms) / 1000.0 BETWEEN 0 AND 90
    )
    SELECT crypto, COUNT(*) AS nb_trades, COUNT(DISTINCT market_id) AS nb_marches,
        ROUND(AVG(price), 4) AS avg_price,
        ROUND(100.0 * SUM(is_win) / COUNT(*), 2) AS win_rate_pct,
        ROUND(SUM(is_win)::FLOAT/COUNT(*)*(1-AVG(price))/AVG(price)*0.98
              -(1-SUM(is_win)::FLOAT/COUNT(*)), 4) AS ev_net
    FROM base
    GROUP BY crypto ORDER BY ev_net DESC
""").df().to_string(index=False))
