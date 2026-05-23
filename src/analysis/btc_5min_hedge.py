"""
Analyse de hedge sur les marchés Bitcoin up/down 5 minutes
==========================================================

Ces marchés sont déterministes : chaque fenêtre de 5 minutes génère un marché
dont le slug est calculable directement depuis le timestamp :
    slug = f"btc-updown-5m-{window_ts}"
    window_ts = timestamp - (timestamp % 300)   ← arrondi à la 5min inférieure

On ne peut pas les trouver via la liste générale des marchés — il faut les
construire et les fetcher un par un.

Objectif : tester si miser sur l'upsider (side majoritaire, prix > 0.5) ou
l'underdog (side minoritaire, prix < 0.5) à différents moments avant la
résolution génère un ROI positif.

Usage :
    python3 src/analysis/btc_5min_hedge.py [--days 7] [--verify-slug]

Options :
    --days N       Analyser les N derniers jours (défaut : 7, max raisonnable : 30)
    --verify-slug  Tester quelques slugs récents avant de lancer l'analyse complète
"""

import sys
import time
import argparse
import requests
import json
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "outputs" / "btc_hedge"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

GAMMA_URL    = "https://gamma-api.polymarket.com/markets/slug"
TRADES_URL   = "https://data-api.polymarket.com/trades"
SESSION      = requests.Session()
SESSION.headers.update({"User-Agent": "btc-hedge-analysis/1.0"})

REQUEST_DELAY         = 0.15   # secondes entre appels séquentiels
MAX_WORKERS           = 8      # threads pour les fetches parallèles
MAX_TRADES_PER_MARKET = 500

# Fenêtres temporelles analysées (secondes avant résolution)
WINDOWS_30S = list(range(300, 120 - 1, -30))   # 300 270 240 210 180 150 120
WINDOWS_10S = list(range(110,  60 - 1, -10))   # 110 100 90 80 70 60
WINDOWS_5S  = list(range( 55,   0 - 1,  -5))   # 55 50 45 ... 10 5 0
ALL_WINDOWS = sorted(set(WINDOWS_30S + WINDOWS_10S + WINDOWS_5S), reverse=True)


# ── Arguments CLI ─────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--days",        type=int,  default=7,     help="Nombre de jours d'historique")
parser.add_argument("--verify-slug", action="store_true",      help="Tester quelques slugs récents")
args = parser.parse_args()


# ── Utilitaires réseau ────────────────────────────────────────────────────────

def fetch_json(url: str, params: dict = None, retries: int = 3) -> dict | list | None:
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=15)
            if r.status_code == 404:
                return None   # marché inexistant = normal
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt < retries - 1:
                time.sleep(1.5 ** attempt)
            else:
                return None
    return None


def window_slug(ts: int) -> str:
    """Calcule le slug d'un marché BTC 5min depuis un timestamp Unix."""
    wt = ts - (ts % 300)
    return f"btc-updown-5m-{wt}"


# ── Étape 0 : Vérification du format de slug ─────────────────────────────────

print("=" * 70)
print("  ANALYSE HEDGE — BITCOIN UP/DOWN 5 MINUTES")
print("=" * 70)

now_ts = int(time.time())

if args.verify_slug:
    print("\n[0] Vérification du format de slug sur les 10 dernières fenêtres...\n")
    found = 0
    for i in range(10):
        ts   = now_ts - i * 300
        wt   = ts - (ts % 300)
        slug = f"btc-updown-5m-{wt}"
        data = fetch_json(f"{GAMMA_URL}/{slug}")
        status = "✓ TROUVÉ" if data else "✗ absent"
        print(f"  {slug}  →  {status}")
        if data:
            found += 1
            print(f"    question : {data.get('question', '')[:65]}")
            print(f"    outcomes : {data.get('outcomes')}")
            print(f"    prices   : {data.get('outcomePrices')}")
        time.sleep(0.2)
    if found == 0:
        print("\n  ⚠  Aucun slug trouvé. Le format est peut-être différent.")
        print("  Essai de variantes...")
        variants = [
            f"bitcoin-up-down-5m-{now_ts - (now_ts % 300)}",
            f"btc-5m-{now_ts - (now_ts % 300)}",
            f"will-btc-be-higher-5m-{now_ts - (now_ts % 300)}",
        ]
        for v in variants:
            data = fetch_json(f"{GAMMA_URL}/{v}")
            print(f"  {v}  →  {'✓' if data else '✗'}")
        sys.exit(0)
    print(f"\n  Format confirmé : btc-updown-5m-{{timestamp}}")


# ── Étape 1 : Générer les timestamps historiques ─────────────────────────────

print(f"\n[1/4] Génération des fenêtres sur {args.days} jours...")

end_ts   = now_ts - (now_ts % 300)            # dernière fenêtre complète
start_ts = end_ts - args.days * 86400          # N jours en arrière

# Une fenêtre toutes les 300 secondes
all_window_ts = list(range(start_ts, end_ts, 300))
print(f"  Fenêtres à tester : {len(all_window_ts):,}  ({args.days} jours × 288 par jour)")


# ── Étape 2 : Fetcher les métadonnées des marchés en parallèle ───────────────

print(f"\n[2/4] Fetch des marchés (threads={MAX_WORKERS})...")
print("  (chaque marché inexistant est ignoré silencieusement)\n")

def fetch_market(wt: int) -> dict | None:
    slug = f"btc-updown-5m-{wt}"
    data = fetch_json(f"{GAMMA_URL}/{slug}")
    if not data:
        return None
    # Vérifier que c'est bien un marché résolu
    if not data.get("closed"):
        return None
    # Extraire la résolution depuis outcomePrices
    prices = data.get("outcomePrices")
    if isinstance(prices, str):
        try: prices = json.loads(prices)
        except Exception: prices = None
    if not prices or len(prices) < 2:
        return None
    try:
        p0, p1 = float(prices[0]), float(prices[1])
    except (TypeError, ValueError):
        return None
    if p0 >= 0.99:
        yes_won = 1.0   # Up gagne (index 0)
    elif p1 >= 0.99:
        yes_won = 0.0   # Down gagne (index 1)
    else:
        return None     # résolution ambiguë

    # conditionId pour fetcher les trades ensuite
    cid = data.get("conditionId") or data.get("id")
    if not cid:
        return None

    end_date = data.get("endDateIso") or data.get("endDate")
    return {
        "condition_id": str(cid),
        "window_ts":    wt,
        "end_date":     end_date,
        "yes_won":      yes_won,
        "question":     data.get("question", "")[:60],
        "outcomes":     data.get("outcomes"),
    }

markets = []
errors  = 0
completed = 0

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
    futures = {ex.submit(fetch_market, wt): wt for wt in all_window_ts}
    for fut in as_completed(futures):
        completed += 1
        result = fut.result()
        if result:
            markets.append(result)
        if completed % 100 == 0:
            print(f"  {completed}/{len(all_window_ts)}  →  {len(markets)} marchés résolus trouvés", end="\r")

print(f"\n  Marchés résolus trouvés : {len(markets)}")

if len(markets) == 0:
    print("\n  ⚠  Aucun marché trouvé. Lancez avec --verify-slug pour diagnostiquer.")
    print("  Conseil : vérifiez que le format btc-updown-5m-{ts} est correct.\n")
    sys.exit(1)

# Exemples
print(f"\n  Exemples (5 premiers) :")
for m in markets[:5]:
    print(f"    [{m['window_ts']}]  {m['question']}  →  {'UP' if m['yes_won'] else 'DOWN'}")


# ── Étape 3 : Fetcher les trades pour chaque marché ──────────────────────────

print(f"\n[3/4] Fetch des trades ({len(markets)} marchés)...")

def fetch_trades_for_market(m: dict) -> list[dict]:
    cid    = m["condition_id"]
    yes_won = m["yes_won"]
    try:
        end_dt = pd.Timestamp(m["end_date"], tz="UTC")
    except Exception:
        end_dt = pd.Timestamp(m["window_ts"] + 300, unit="s", tz="UTC")

    params = {
        "market": cid,
        "limit":  MAX_TRADES_PER_MARKET,
        "before": int(end_dt.timestamp()),
        "after":  int((end_dt - pd.Timedelta(seconds=600)).timestamp()),
    }
    data = fetch_json(TRADES_URL, params)
    if not data:
        return []

    trades_raw = data if isinstance(data, list) else data.get("trades", [])
    rows = []
    for t in trades_raw:
        ts_val = t.get("timestamp") or t.get("created_at") or t.get("ts")
        if ts_val is None:
            continue
        try:
            ts = pd.Timestamp(ts_val, unit="s", tz="UTC") if isinstance(ts_val, (int, float)) \
                 else pd.Timestamp(ts_val, tz="UTC")
        except Exception:
            continue

        price_val = t.get("price")
        if price_val is None:
            continue
        try:
            price = float(price_val)
        except (TypeError, ValueError):
            continue

        # Si outcomeIndex=1 → prix du token Down → convertir en prix Up (=YES)
        oi = t.get("outcomeIndex") or t.get("outcome_index")
        if oi is not None:
            try:
                if int(oi) == 1:
                    price = 1.0 - price
            except (TypeError, ValueError):
                pass

        secs_before = (end_dt - ts).total_seconds()
        if not (0 <= secs_before <= 600):
            continue

        rows.append({
            "condition_id": cid,
            "ts":           ts,
            "price_yes":    price,
            "yes_won":      yes_won,
            "end_dt":       end_dt,
            "secs_before":  secs_before,
        })
    return rows

all_trades = []
done = 0

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
    futures = {ex.submit(fetch_trades_for_market, m): m for m in markets}
    for fut in as_completed(futures):
        rows = fut.result()
        all_trades.extend(rows)
        done += 1
        if done % 50 == 0:
            print(f"  {done}/{len(markets)} marchés  →  {len(all_trades):,} trades", end="\r")

print(f"\n  Trades récupérés : {len(all_trades):,}  pour {len(set(t['condition_id'] for t in all_trades))} marchés")

if len(all_trades) == 0:
    print("  ⚠  Aucun trade récupéré. Vérifier l'endpoint data-api.polymarket.com/trades.")
    sys.exit(1)

df_trades = pd.DataFrame(all_trades)
n_markets_usable = df_trades["condition_id"].nunique()


# ── Étape 4 : Analyse par fenêtre temporelle ─────────────────────────────────

print(f"\n[4/4] Calcul ROI sur {n_markets_usable} marchés, {len(ALL_WINDOWS)} fenêtres temporelles...")

results = []

for window_sec in ALL_WINDOWS:
    # Dernier trade connu à au moins window_sec secondes de la fin
    sub = df_trades[df_trades["secs_before"] >= window_sec]
    if len(sub) < 5:
        continue

    # Un point par marché : le trade le plus proche de la fenêtre
    last = (
        sub.sort_values("secs_before")
           .groupby("condition_id")
           .first()
           [["price_yes", "yes_won"]]
           .reset_index()
    )
    if len(last) < 5:
        continue

    p_yes   = last["price_yes"].values.astype(float)
    yes_won = last["yes_won"].values.astype(float)

    # Upsider = side avec prix > 0.5 à cet instant
    yes_is_upsider = p_yes > 0.5
    upsider_wins   = np.where(yes_is_upsider, yes_won == 1, yes_won == 0)
    underdog_wins  = ~upsider_wins

    p_up  = np.clip(np.where(yes_is_upsider, p_yes, 1.0 - p_yes), 0.01, 0.99)
    p_dog = np.clip(1.0 - p_up, 0.01, 0.99)

    # ROI : win_rate × (1/P − 1) − (1 − win_rate)
    roi_up  = np.where(upsider_wins,  1.0/p_up  - 1.0, -1.0)
    roi_dog = np.where(underdog_wins, 1.0/p_dog - 1.0, -1.0)

    results.append({
        "secs_before":      window_sec,
        "n_markets":        len(last),
        "p_yes_mean":       p_yes.mean(),
        "p_yes_std":        p_yes.std(),
        "wr_upsider":       upsider_wins.mean(),
        "wr_underdog":      underdog_wins.mean(),
        "roi_upsider":      roi_up.mean(),
        "roi_underdog":     roi_dog.mean(),
        "roi_upsider_pct":  roi_up.mean()  * 100,
        "roi_underdog_pct": roi_dog.mean() * 100,
    })

df = pd.DataFrame(results).sort_values("secs_before", ascending=False)


# ── Tableau bilan ─────────────────────────────────────────────────────────────

def fmt(s: int) -> str:
    s = int(s)
    return f"{s//60}m{s%60:02d}s" if s >= 60 else f"    {s}s"

print("\n" + "=" * 108)
print("  BILAN — HEDGE SUR MARCHÉS BITCOIN UP/DOWN 5 MINUTES")
print("=" * 108)
print(f"\n  Marchés analysés : {n_markets_usable}  |  Période : {args.days} jours  |  Fenêtres : {len(df)}\n")

print(f"  {'Temps avant':>12}  {'N':>5}  {'P_up':>6}  "
      f"{'WR upsider':>10}  {'ROI upsider':>11}  "
      f"{'WR underdog':>11}  {'ROI underdog':>12}")
print("  " + "-" * 102)

for _, row in df.iterrows():
    up_ok  = "✓" if row["roi_upsider"]  > 0 else " "
    dog_ok = "✓" if row["roi_underdog"] > 0 else " "
    print(
        f"  {fmt(row['secs_before']):>12}  "
        f"{int(row['n_markets']):>5}  "
        f"{row['p_yes_mean']:>5.1%}  "
        f"  {row['wr_upsider']:>8.1%}  "
        f"  {up_ok}{row['roi_upsider_pct']:>9.1f}%  "
        f"  {row['wr_underdog']:>9.1%}  "
        f"  {dog_ok}{row['roi_underdog_pct']:>9.1f}%"
    )

# ── Interprétation ────────────────────────────────────────────────────────────

print("\n" + "=" * 108)
print("  INTERPRÉTATION")
print("=" * 108)

if len(df) > 0:
    best_up  = df.loc[df["roi_upsider"].idxmax()]
    best_dog = df.loc[df["roi_underdog"].idxmax()]

    print(f"\n  Meilleur ROI upsider  : {best_up['roi_upsider_pct']:+.1f}% "
          f"à {fmt(best_up['secs_before'])} (WR={best_up['wr_upsider']:.1%}, N={int(best_up['n_markets'])})")
    print(f"  Meilleur ROI underdog : {best_dog['roi_underdog_pct']:+.1f}% "
          f"à {fmt(best_dog['secs_before'])} (WR={best_dog['wr_underdog']:.1%}, N={int(best_dog['n_markets'])})")

    for label, col, wr_col in [("upsider",  "roi_upsider",  "wr_upsider"),
                                 ("underdog", "roi_underdog", "wr_underdog")]:
        pos = df[df[col] > 0.02]
        print(f"\n  Fenêtres avec ROI {label} > 2% : {len(pos)}")
        for _, r in pos.iterrows():
            print(f"    → {fmt(r['secs_before']):<8}  ROI={r[col+'_pct']:+.1f}%  "
                  f"WR={r[wr_col]:.1%}  N={int(r['n_markets'])}")

# ── Sauvegarde ────────────────────────────────────────────────────────────────

csv_path = OUTPUT_DIR / "btc_5min_hedge_results.csv"
df.to_csv(csv_path, index=False)
print(f"\n  CSV : {csv_path}")

# ── Graphiques ────────────────────────────────────────────────────────────────

if len(df) >= 3:
    fig, axes = plt.subplots(2, 2, figsize=(16, 10))
    fig.suptitle(f"Bitcoin up/down 5min — {n_markets_usable} marchés, {args.days} jours",
                 fontsize=14, fontweight="bold")
    x = df["secs_before"].values

    ax = axes[0, 0]
    ax.plot(x, df["wr_upsider"]  * 100, "b-o", ms=4, label="Upsider (majoritaire)")
    ax.plot(x, df["wr_underdog"] * 100, "r-o", ms=4, label="Underdog (minoritaire)")
    ax.axhline(50, color="gray", ls="--", alpha=0.5, label="50%")
    ax.set_title("Win rate"); ax.set_xlabel("Secondes avant résolution")
    ax.set_ylabel("Win rate (%)"); ax.legend(); ax.set_xlim(max(x), 0)
    ax.grid(True, alpha=0.3); ax.yaxis.set_major_formatter(mtick.PercentFormatter())

    ax = axes[0, 1]
    ax.plot(x, df["roi_upsider_pct"],  "b-o", ms=4, label="Upsider")
    ax.plot(x, df["roi_underdog_pct"], "r-o", ms=4, label="Underdog")
    ax.axhline(0, color="black", lw=1.5)
    ax.fill_between(x, df["roi_upsider_pct"],  0, where=(df["roi_upsider_pct"]  > 0), alpha=0.15, color="blue")
    ax.fill_between(x, df["roi_underdog_pct"], 0, where=(df["roi_underdog_pct"] > 0), alpha=0.15, color="red")
    ax.set_title("ROI moyen"); ax.set_xlabel("Secondes avant résolution")
    ax.set_ylabel("ROI (%)"); ax.legend(); ax.set_xlim(max(x), 0)
    ax.grid(True, alpha=0.3); ax.yaxis.set_major_formatter(mtick.PercentFormatter())

    ax = axes[1, 0]
    ax.plot(x, df["p_yes_mean"] * 100, "g-o", ms=4)
    ax.fill_between(x,
                    (df["p_yes_mean"] - df["p_yes_std"]) * 100,
                    (df["p_yes_mean"] + df["p_yes_std"]) * 100,
                    alpha=0.15, color="green", label="±1σ")
    ax.axhline(50, color="gray", ls="--", alpha=0.5)
    ax.set_title("Prix Up moyen"); ax.set_xlabel("Secondes avant résolution")
    ax.set_ylabel("Prix Up (%)"); ax.legend(); ax.set_xlim(max(x), 0)
    ax.grid(True, alpha=0.3); ax.yaxis.set_major_formatter(mtick.PercentFormatter())

    ax = axes[1, 1]
    ax.bar(range(len(x)), df["n_markets"].values, color="steelblue", alpha=0.7)
    ax.set_xticks(range(len(x)))
    ax.set_xticklabels([fmt(s) for s in x], rotation=90, fontsize=7)
    ax.set_title("Marchés avec données par fenêtre")
    ax.set_xlabel("Fenêtre"); ax.set_ylabel("N marchés"); ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    png_path = OUTPUT_DIR / "btc_5min_hedge_chart.png"
    plt.savefig(png_path, dpi=150, bbox_inches="tight")
    print(f"  PNG : {png_path}")
    plt.close()

print("\nAnalyse terminée.\n")
