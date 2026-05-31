import duckdb, requests
conn = duckdb.connect()

# Texte exact des questions dans nos données
print("=== QUESTIONS DANS NOS DONNEES ===")
df = conn.execute("""
    SELECT market_id, question, crypto
    FROM read_parquet('data/updown/markets_5m.parquet')
    WHERE resolution IN (0,1)
    LIMIT 10
""").df()
print(df.to_string(index=False))

# Ce que l'API Gamma retourne sur les premiers marchés fermés
print()
print("=== PREMIERES QUESTIONS API GAMMA (closed=true) ===")
resp = requests.get(
    "https://gamma-api.polymarket.com/markets",
    params={"closed": "true", "limit": 20, "offset": 0},
    timeout=15
)
data = resp.json()
for m in data[:10]:
    print(f"  id={m.get('id')} | q={str(m.get('question',''))[:60]}")
