"""
Diagnostic : marchés BTC up/down 5 minutes
Exécute : python3 src/analysis/btc_diag.py
"""
import time, requests, json
from datetime import datetime, timezone

S = requests.Session()
now = int(time.time())

# ── 1. Tester les 20 dernières fenêtres ──────────────────────────────────────
print("=== 1. Test slugs récents ===")
found = []
for i in range(20):
    wt   = (now - i * 300) - ((now - i * 300) % 300)
    slug = f"btc-updown-5m-{wt}"
    r    = S.get(f"https://gamma-api.polymarket.com/markets/slug/{slug}", timeout=10)
    dt   = datetime.fromtimestamp(wt, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    if r.status_code == 200:
        d = r.json()
        print(f"  ✓ {slug}  [{dt}]  closed={d.get('closed')}  prices={d.get('outcomePrices')}")
        found.append((slug, wt, d))
    else:
        print(f"  ✗ {slug}  [{dt}]  HTTP {r.status_code}")
    time.sleep(0.15)

if not found:
    print("\n  AUCUN slug trouvé — format peut-être différent. Cherche un marché BTC actif...")
    # Chercher un marché BTC actif via la liste générale
    r = S.get("https://gamma-api.polymarket.com/markets",
              params={"active": "true", "limit": 100}, timeout=15)
    data = r.json() if r.status_code == 200 else []
    page = data if isinstance(data, list) else data.get("markets", [])
    for m in page:
        q = (m.get("question") or "").lower()
        s = (m.get("slug") or "").lower()
        if "bitcoin" in q or "btc" in q or "btc" in s:
            print(f"  → slug={m.get('slug')}  question={m.get('question','')[:60]}")
    import sys; sys.exit(0)

# ── 2. Inspecter le premier marché trouvé ────────────────────────────────────
slug0, wt0, m0 = found[0]
cid = m0.get("conditionId") or m0.get("id")
print(f"\n=== 2. Détail marché : {slug0} ===")
print(f"  conditionId  : {cid}")
print(f"  closed       : {m0.get('closed')}")
print(f"  active       : {m0.get('active')}")
print(f"  outcomePrices: {m0.get('outcomePrices')}")
print(f"  outcomes     : {m0.get('outcomes')}")
print(f"  endDate      : {m0.get('endDate')}")
print(f"  endDateIso   : {m0.get('endDateIso')}")
print(f"  createdAt    : {m0.get('createdAt')}")
print(f"  clobTokenIds : {m0.get('clobTokenIds')}")

# ── 3. Tester les trades sans filtre date ────────────────────────────────────
print(f"\n=== 3. Trades bruts (sans filtre) pour {cid[:20]}... ===")
r = S.get("https://data-api.polymarket.com/trades",
          params={"market": cid, "limit": 10}, timeout=15)
print(f"  HTTP {r.status_code}")
if r.status_code == 200:
    trades = r.json()
    if isinstance(trades, dict):
        print(f"  Clés réponse : {list(trades.keys())}")
        trades = trades.get("trades", trades.get("data", []))
    print(f"  Nombre de trades : {len(trades)}")
    if trades:
        t0 = trades[0]
        print(f"  Clés d'un trade : {list(t0.keys())}")
        print(f"  Exemple trade   : {json.dumps(t0, indent=2)[:500]}")
else:
    print(f"  Réponse : {r.text[:200]}")

# ── 4. Tester avec before/after ──────────────────────────────────────────────
end_ts = wt0 + 300
print(f"\n=== 4. Trades avec before={end_ts}, after={end_ts-600} ===")
r = S.get("https://data-api.polymarket.com/trades",
          params={"market": cid, "limit": 500, "before": end_ts, "after": end_ts - 600},
          timeout=15)
print(f"  HTTP {r.status_code}")
if r.status_code == 200:
    trades = r.json()
    if isinstance(trades, dict):
        trades = trades.get("trades", trades.get("data", []))
    print(f"  Nombre de trades : {len(trades)}")
    if trades:
        for t in trades[:3]:
            ts_raw = t.get("timestamp") or t.get("created_at") or t.get("ts") or "?"
            print(f"    ts={ts_raw}  price={t.get('price')}  size={t.get('size')}")

# ── 5. Tester l'endpoint CLOB price-history ──────────────────────────────────
token_ids = m0.get("clobTokenIds")
if isinstance(token_ids, str):
    try: token_ids = json.loads(token_ids)
    except: token_ids = None
if token_ids and len(token_ids) > 0:
    token = token_ids[0]
    print(f"\n=== 5. CLOB price-history pour token {token[:20]}... ===")
    r = S.get("https://clob.polymarket.com/prices-history",
              params={"market": token, "startTs": wt0, "endTs": wt0 + 300, "fidelity": 1},
              timeout=15)
    print(f"  HTTP {r.status_code}")
    if r.status_code == 200:
        h = r.json()
        print(f"  Clés réponse : {list(h.keys()) if isinstance(h, dict) else type(h)}")
        pts = h.get("history") or h.get("prices") or (h if isinstance(h, list) else [])
        print(f"  Nombre de points : {len(pts)}")
        for p in pts[:5]:
            print(f"    {p}")
    else:
        print(f"  Réponse : {r.text[:200]}")

    # Aussi tester avec "tokenID" au lieu de "market"
    print(f"\n=== 5b. CLOB price-history param tokenID ===")
    r = S.get("https://clob.polymarket.com/prices-history",
              params={"tokenID": token, "startTs": wt0, "endTs": wt0 + 300, "fidelity": 1},
              timeout=15)
    print(f"  HTTP {r.status_code}  →  {r.text[:300]}")

print("\nDiagnostic terminé.")
