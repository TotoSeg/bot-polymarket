"""
Diagnostic xtracker.polymarket.com — valide la structure réelle des réponses API.
Usage : python src/phase6_bot/diag_xtracker.py
"""
import json, requests
from pprint import pprint

BASE = "https://xtracker.polymarket.com/api"

def _get(path, params=None):
    r = requests.get(f"{BASE}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def show(label, data, n=2):
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    items = data if isinstance(data, list) else data.get("data", [data])
    print(f"  type root : {type(data).__name__}")
    if isinstance(data, dict):
        print(f"  cles root : {list(data.keys())}")
    if items:
        print(f"  {len(items)} item(s). Premiers :")
        for item in items[:n]:
            pprint(item, depth=4, width=120)
            print("---")

# 1. /trackings
data = _get("/trackings", {"activeOnly": "true"})
show("/trackings?activeOnly=true", data)

items = data if isinstance(data, list) else data.get("data", [])
if not items:
    print("[WARN] Aucun tracking actif retourné")
else:
    first = items[0]
    # Tester les champs critiques
    print("\n  Champs critiques premier item :")
    for key in ["handle", "user", "endDate", "end_date", "endAt", "startDate",
                "start_date", "marketLink", "market_link", "url", "slug", "isActive", "active"]:
        if key in first:
            print(f"    OK {key!r:20s} = {str(first[key])[:80]}")
    user = first.get("user") or {}
    if isinstance(user, dict):
        print(f"    user keys : {list(user.keys())}")

    # 2. /users/{handle}/posts
    handle = (first.get("user") or {}).get("handle") or first.get("handle", "")
    if not handle:
        # cherche dans tous les champs
        for k, v in first.items():
            if isinstance(v, str) and v.startswith("@"):
                handle = v.lstrip("@")
                break
    if handle:
        print(f"\n  Handle détecté : @{handle}")
        posts_data = _get(f"/users/{handle}/posts",
                          {"startDate": "2024-01-01", "endDate": "2025-01-01"})
        show(f"/users/{handle}/posts (2024)", posts_data)
        posts = posts_data if isinstance(posts_data, list) else posts_data.get("data", [])
        if posts:
            print(f"  → Champs d'un post : {list(posts[0].keys())}")

        # 3. /users/{handle}/trackings
        tr_data = _get(f"/users/{handle}/trackings", {"activeOnly": "false"})
        show(f"/users/{handle}/trackings", tr_data)
        tr_items = tr_data if isinstance(tr_data, list) else tr_data.get("data", [])
        if tr_items:
            print(f"  → Champs d'un tracking : {list(tr_items[0].keys())}")
    else:
        print("  [WARN] Pas de handle trouvé dans le premier tracking")
