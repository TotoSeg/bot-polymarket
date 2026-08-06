"""
Recherche des marches 5m up/down dans data/markets.parquet et data/quant.parquet
================================================================================
Objectif : trouver si les marches "Bitcoin Up or Down 5 minutes" sont deja dans
les donnees historiques 268K marches / 170M trades.
"""
import sys, duckdb
import pandas as pd

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

conn = duckdb.connect()

print("=" * 60)
print("RECHERCHE MARCHES 5M UP/DOWN DANS MARKETS.PARQUET")
print("=" * 60)

# 1. Chercher dans markets.parquet les marches 5 minutes up/down
print("\n[1] Scan markets.parquet pour mots-cles 5m up/down...")
df_markets = conn.execute("""
    SELECT *
    FROM read_parquet('data/markets.parquet')
    WHERE (
        lower(question) LIKE '%up or down%'
        OR lower(question) LIKE '%will btc%'
        OR lower(question) LIKE '%will bitcoin%'
        OR lower(question) LIKE '%will eth%'
        OR lower(question) LIKE '%will sol%'
    )
    AND (
        lower(question) LIKE '%5 min%'
        OR lower(question) LIKE '%5min%'
        OR lower(question) LIKE '%5-min%'
        OR lower(question) LIKE '%next 5%'
    )
    LIMIT 500
""").df()

print(f"  Marches trouves dans markets.parquet : {len(df_markets)}")

if len(df_markets) > 0:
    print("\n  Exemples de questions :")
    for q in df_markets["question"].head(10).tolist():
        print(f"    - {q[:100]}")
    print(f"\n  Colonnes disponibles : {list(df_markets.columns)}")
    print(f"\n  Plage de dates :")
    for col in ["end_date", "end_date_iso", "end_time", "resolution_time"]:
        if col in df_markets.columns:
            print(f"    {col}: {df_markets[col].min()} -> {df_markets[col].max()}")

    # Sauvegarder
    df_markets.to_parquet("data/updown/markets_5m_historical.parquet", index=False)
    print(f"\n  Sauvegarde -> data/updown/markets_5m_historical.parquet")
else:
    # Essai avec mots-cles plus larges
    print("  Aucun resultat. Essai avec mots-cles plus larges...")
    df_sample = conn.execute("""
        SELECT question, COUNT(*) as n
        FROM read_parquet('data/markets.parquet')
        WHERE lower(question) LIKE '%5 minute%'
            OR lower(question) LIKE '%5min%'
        GROUP BY question
        ORDER BY n DESC
        LIMIT 20
    """).df()
    print(f"  Marches '5 minute' : {len(df_sample)}")
    for _, row in df_sample.iterrows():
        print(f"    [{row['n']:>4}x] {row['question'][:100]}")

    # Essai up/down en general
    df_ud = conn.execute("""
        SELECT question, COUNT(*) as n
        FROM read_parquet('data/markets.parquet')
        WHERE lower(question) LIKE '%up or down%'
        GROUP BY question
        ORDER BY n DESC
        LIMIT 20
    """).df()
    print(f"\n  Marches 'up or down' : {len(df_ud)}")
    for _, row in df_ud.iterrows():
        print(f"    [{row['n']:>4}x] {row['question'][:100]}")

# 2. Si on a trouve des marches, chercher dans quant.parquet
print("\n" + "=" * 60)
print("[2] SCAN QUANT.PARQUET")
print("=" * 60)

# D'abord, inspecter la structure de quant.parquet
print("\n  Structure quant.parquet :")
df_schema = conn.execute("DESCRIBE SELECT * FROM read_parquet('data/quant.parquet') LIMIT 1").df()
print(df_schema.to_string())

# Compter les lignes totales
print("\n  Comptage total...")
n_total = conn.execute("SELECT COUNT(*) FROM read_parquet('data/quant.parquet')").fetchone()[0]
print(f"  Total lignes quant.parquet : {n_total:,}")

if len(df_markets) > 0:
    # Identifier la colonne de jointure
    market_cols = [c for c in df_markets.columns if "id" in c.lower() or "condition" in c.lower() or "token" in c.lower()]
    print(f"\n  Colonnes ID dans markets : {market_cols}")

    quant_cols = conn.execute("SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet('data/quant.parquet') LIMIT 1)").df()
    print(f"  Colonnes quant.parquet : {quant_cols['column_name'].tolist()}")

# 3. Chercher directement dans quant.parquet si market_id matche
# (essai sur un sample pour reperer les IDs)
print("\n  Sample quant.parquet (5 lignes) :")
df_q_sample = conn.execute("SELECT * FROM read_parquet('data/quant.parquet') LIMIT 5").df()
print(df_q_sample.to_string())

# 4. Si les IDs des marches 5m sont connus, compter les trades
if len(df_markets) > 0:
    # Trouver la colonne ID principale
    id_col = None
    for col in ["condition_id", "market_id", "id"]:
        if col in df_markets.columns:
            id_col = col
            break

    if id_col:
        ids = df_markets[id_col].dropna().tolist()[:100]
        print(f"\n  Recherche dans quant.parquet pour {len(ids)} market IDs...")

        # Trouver la colonne correspondante dans quant
        quant_id_col = None
        for col in ["market_id", "condition_id", "id"]:
            try:
                test = conn.execute(f"SELECT {col} FROM read_parquet('data/quant.parquet') LIMIT 1").df()
                quant_id_col = col
                break
            except:
                pass

        if quant_id_col:
            ids_str = ", ".join(f"'{i}'" for i in ids[:50])
            n_trades = conn.execute(f"""
                SELECT COUNT(*)
                FROM read_parquet('data/quant.parquet')
                WHERE {quant_id_col} IN ({ids_str})
            """).fetchone()[0]
            print(f"  Trades trouves : {n_trades:,}")
        else:
            print("  Impossible de trouver la colonne ID dans quant.parquet")

print("\nTermine.")
