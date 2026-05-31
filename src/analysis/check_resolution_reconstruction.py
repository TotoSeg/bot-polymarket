import duckdb
conn = duckdb.connect()

print("=== VALIDATION : reconstruction vs resolution connue ===")
df = conn.execute("""
    WITH spot AS (
        SELECT
            t.market_id,
            t.crypto,
            FIRST(t.spot_price_usdt ORDER BY t.timestamp_ms ASC) AS price_open,
            LAST(t.spot_price_usdt  ORDER BY t.timestamp_ms ASC) AS price_close,
            COUNT(*) as nb_ticks
        FROM read_parquet('data/updown/ticks_5m.parquet') t
        WHERE t.timestamp_ms > 0
          AND t.spot_price_usdt IS NOT NULL
          AND t.spot_price_usdt > 0
        GROUP BY t.market_id, t.crypto
    ),
    labeled AS (
        SELECT
            s.crypto,
            CASE WHEN s.price_close >= s.price_open THEN 1 ELSE 0 END AS resolution_calc,
            m.resolution AS resolution_true
        FROM spot s
        INNER JOIN read_parquet('data/updown/markets_5m.parquet') m
            ON s.market_id = m.market_id
        WHERE m.resolution IN (0, 1)
    )
    SELECT
        crypto,
        COUNT(*) as nb_marches,
        SUM(CASE WHEN resolution_calc = resolution_true THEN 1 ELSE 0 END) as correct,
        ROUND(100.0 * SUM(CASE WHEN resolution_calc = resolution_true THEN 1 ELSE 0 END) / COUNT(*), 2) as accuracy_pct
    FROM labeled
    GROUP BY crypto ORDER BY crypto
""").df()
print(df.to_string(index=False))

print()
print("=== MARCHES LABELISABLES SUR TOUT LE DATASET ===")
df2 = conn.execute("""
    WITH spot AS (
        SELECT DISTINCT market_id, crypto
        FROM read_parquet('data/updown/ticks_5m.parquet')
        WHERE timestamp_ms > 0 AND spot_price_usdt > 0
    )
    SELECT
        m.crypto,
        COUNT(DISTINCT m.market_id) as total_marches,
        COUNT(DISTINCT s.market_id) as labelisables,
        SUM(CASE WHEN m.resolution IN (0,1) THEN 1 ELSE 0 END) as deja_connus
    FROM read_parquet('data/updown/markets_5m.parquet') m
    LEFT JOIN spot s ON m.market_id = s.market_id
    GROUP BY m.crypto ORDER BY m.crypto
""").df()
print(df2.to_string(index=False))
