"""
Analyse de hedge sur les marchés Bitcoin up/down 5 minutes
==========================================================
Récupère les données via les APIs Polymarket (pas besoin de fichiers locaux).

Sources :
  - Gamma API  : liste des marchés Bitcoin up/down résolus
  - Data API   : trades individuels par marché (timestamps + prix)

Usage :
  python3 src/analysis/btc_5min_hedge.py

Durée estimée : 5-20 minutes selon le nombre de marchés trouvés.
"""

import requests
import time
import json
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mtick
from pathlib import Path
from datetime import datetime, timezone

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "outputs" / "btc_hedge"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

GAMMA_URL   = "https://gamma-api.polymarket.com/markets"
TRADES_URL  = "https://data-api.polymarket.com/trades"

# Garde-fous API
REQUEST_DELAY  = 0.3   # secondes entre chaque appel
MAX_MARKETS    = 2000  # limite de sécurité
MAX_TRADES_PER_MARKET = 500

# Fenêtres temporelles (secondes avant résolution)
WINDOWS_30S = list(range(300, 120 - 1, -30))
WINDOWS_10S = list(range(110,  60 - 1, -10))
WINDOWS_5S  = list(range( 55,   0 - 1,  -5))
ALL_WINDOWS = sorted(set(WINDOWS_30S + WINDOWS_10S + WINDOWS_5S), reverse=True)


# ── Utilitaires ──────────────────────────────────────────────────────────────

def get_json(url: str, params: dict, retries: int = 3) -> list | dict | None:
    """GET JSON avec retry automatique."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                print(f"    ✗ Erreur {url} params={params} : {e}")
                return None


def is_btc_5min(market: dict) -> bool:
    """Retourne True si le marché est un Bitcoin up/down de ~5 minutes."""
    q    = (market.get("question")   or "").lower()
    slug = (market.get("market_slug") or market.get("slug") or "").lower()
    text = q + " " + slug

    # Doit contenir bitcoin ou btc
    has_btc = "bitcoin" in text or " btc " in text or "btc-" in text or "-btc" in text

    # Doit contenir un indicateur directionnel
    has_dir = any(kw in text for kw in [
        "up or down", "up-or-down", "higher or lower", "higher-or-lower",
        "above or below", "above-or-below", "go up", "go down",
        "increase", "decrease", "pump", "dump", "bull", "bear",
        "rise", "fall", "gain", "drop",
    ])

    if not (has_btc and has_dir):
        return False

    # Vérifier la durée (~5 minutes = 240-480s, tolérance élargie à 60-900s)
    start = market.get("startDate") or market.get("start_date_iso")
    end   = market.get("endDate")   or market.get("end_date_iso")
    if not start or not end:
        return False
    try:
        t_start = pd.Timestamp(start, tz="UTC")
        t_end   = pd.Timestamp(end,   tz="UTC")
        duration_sec = (t_end - t_start).total_seconds()
        return 60 <= duration_sec <= 900
    except Exception:
        return False


# ── Étape 1 : Récupérer les marchés ──────────────────────────────────────────

print(f"{'='*70}")
print("  ANALYSE HEDGE — BITCOIN UP/DOWN 5 MINUTES")
print(f"{'='*70}")
print("\n[1/4] Récupération des marchés Bitcoin up/down (Gamma API)...")

btc_markets = []
offset = 0
page_size = 100
consecutive_empty = 0

while len(btc_markets) < MAX_MARKETS:
    params = {
        "closed":   "true",    # marchés résolus uniquement
        "limit":    page_size,
        "offset":   offset,
        "tag_slug": "crypto",  # filtre large, on affine localement
    }
    data = get_json(GAMMA_URL, params)
    if not data:
        break

    # La réponse peut être une liste ou un dict avec une clé "markets"
    page = data if isinstance(data, list) else data.get("markets", [])
    if not page:
        consecutive_empty += 1
        if consecutive_empty >= 3:
            break
        offset += page_size
        continue

    consecutive_empty = 0
    for m in page:
        if is_btc_5min(m):
            btc_markets.append(m)

    offset += len(page)
    print(f"  page offset={offset:>5}  →  {len(btc_markets)} marchés BTC 5min trouvés", end="\r")

    if len(page) < page_size:
        break  # dernière page
    time.sleep(REQUEST_DELAY)

print(f"\n  Total marchés Bitcoin up/down ~5min : {len(btc_markets)}")

if len(btc_markets) == 0:
    # Tentative sans filtre tag pour diagnostic
    print("  ⚠ Aucun marché trouvé avec tag=crypto. Essai sans filtre tag...")
    params = {"closed": "true", "limit": 10, "offset": 0}
    sample = get_json(GAMMA_URL, params)
    if sample:
        page = sample if isinstance(sample, list) else sample.get("markets", [])
        if page:
            print(f"  Clés disponibles dans un marché : {list(page[0].keys())}")
            print(f"  Exemple : {page[0].get('question', '')}")
    import sys; sys.exit(1)

# Afficher les 5 premiers exemples
print(f"\n  Exemples :")
for m in btc_markets[:5]:
    q   = m.get("question", "")[:65]
    end = m.get("endDate") or m.get("end_date_iso", "")
    dur = ""
    try:
        s = pd.Timestamp(m.get("startDate") or m.get("start_date_iso"), tz="UTC")
        e = pd.Timestamp(end, tz="UTC")
        dur = f"{int((e-s).total_seconds())}s"
    except Exception:
        pass
    print(f"    [{dur:>5}]  {q}")


# ── Étape 2 : Extraire les identifiants et résolutions ───────────────────────

print("\n[2/4] Extraction des métadonnées...")

def extract_condition_id(m: dict) -> str | None:
    for key in ["conditionId", "condition_id", "id"]:
        v = m.get(key)
        if v:
            return str(v)
    # Chercher dans clobTokenIds
    ids = m.get("clobTokenIds") or m.get("clob_token_ids")
    if ids:
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except Exception:
                pass
        if isinstance(ids, list) and ids:
            return str(ids[0])
    return None

def extract_resolution(m: dict) -> float | None:
    """Retourne 1.0 si YES a gagné, 0.0 si NO a gagné, None si inconnu."""
    # Champ resolution direct
    res = m.get("resolution") or m.get("outcome")
    if res is not None:
        s = str(res).lower().strip()
        if s in ("yes", "1", "1.0", "true"):
            return 1.0
        if s in ("no", "0", "0.0", "false"):
            return 0.0
        try:
            v = float(s)
            if v >= 0.99: return 1.0
            if v <= 0.01: return 0.0
        except ValueError:
            pass

    # Chercher dans les tokens/outcomes
    outcomes = m.get("outcomes")
    if isinstance(outcomes, str):
        try: outcomes = json.loads(outcomes)
        except Exception: outcomes = None
    winner = m.get("winner") or m.get("winnerOutcome")
    if winner and outcomes and isinstance(outcomes, list):
        w = str(winner).lower()
        for i, o in enumerate(outcomes):
            if str(o).lower() == w:
                return 1.0 if i == 0 else 0.0

    return None

meta = []  # liste de dicts {condition_id, end_dt, yes_won, duration_sec}
for m in btc_markets:
    cid = extract_condition_id(m)
    if not cid:
        continue
    res = extract_resolution(m)
    if res is None:
        continue  # résolution inconnue → inutilisable

    end_str   = m.get("endDate") or m.get("end_date_iso", "")
    start_str = m.get("startDate") or m.get("start_date_iso", "")
    try:
        end_dt  = pd.Timestamp(end_str,   tz="UTC")
        start_dt = pd.Timestamp(start_str, tz="UTC")
        duration = int((end_dt - start_dt).total_seconds())
    except Exception:
        continue

    meta.append({
        "condition_id": cid,
        "end_dt":       end_dt,
        "yes_won":      res,
        "duration_sec": duration,
    })

print(f"  Marchés utilisables (résolution connue) : {len(meta)}")
if len(meta) == 0:
    print("  ⚠  Aucun marché avec résolution connue. Vérifier le champ 'resolution' dans l'API.")
    import sys; sys.exit(1)


# ── Étape 3 : Récupérer les trades pour chaque marché ────────────────────────

print(f"\n[3/4] Téléchargement des trades ({len(meta)} marchés)...")
print("  (cela peut prendre quelques minutes...)\n")

all_trades = []  # liste de dicts {condition_id, ts, price_yes, yes_won, end_dt}

for i, m in enumerate(meta):
    cid    = m["condition_id"]
    end_dt = m["end_dt"]
    yes_won = m["yes_won"]

    if (i + 1) % 50 == 0 or i == 0:
        print(f"  {i+1}/{len(meta)}  trades chargés : {len(all_trades):,}", end="\r")

    params = {
        "market":  cid,
        "limit":   MAX_TRADES_PER_MARKET,
        # Trades dans les 10 dernières minutes avant la résolution
        "before":  int(end_dt.timestamp()),
        "after":   int((end_dt - pd.Timedelta(seconds=600)).timestamp()),
    }
    data = get_json(TRADES_URL, params)
    if not data:
        time.sleep(REQUEST_DELAY)
        continue

    trades_raw = data if isinstance(data, list) else data.get("trades", [])
    if not trades_raw:
        time.sleep(REQUEST_DELAY)
        continue

    for t in trades_raw:
        # Extraire le timestamp
        ts_val = t.get("timestamp") or t.get("created_at") or t.get("ts")
        if ts_val is None:
            continue
        try:
            if isinstance(ts_val, (int, float)):
                ts = pd.Timestamp(ts_val, unit="s", tz="UTC")
            else:
                ts = pd.Timestamp(ts_val, tz="UTC")
        except Exception:
            continue

        # Extraire le prix YES
        # quant.parquet utilise la perspective YES unifiée
        price_val = t.get("price") or t.get("price_yes")
        if price_val is None:
            continue
        try:
            price = float(price_val)
        except (TypeError, ValueError):
            continue

        # Vérifier que c'est bien le prix YES (pas NO)
        # Dans data-api, outcome_index=0 = YES, outcome_index=1 = NO
        outcome_idx = t.get("outcomeIndex") or t.get("outcome_index")
        if outcome_idx is not None:
            try:
                if int(outcome_idx) == 1:
                    price = 1.0 - price  # convertir en prix YES
            except (TypeError, ValueError):
                pass

        secs_before = (end_dt - ts).total_seconds()
        if secs_before < 0 or secs_before > 600:
            continue

        all_trades.append({
            "condition_id": cid,
            "ts":           ts,
            "price_yes":    price,
            "yes_won":      yes_won,
            "end_dt":       end_dt,
            "secs_before":  secs_before,
        })

    time.sleep(REQUEST_DELAY)

print(f"\n  Trades récupérés : {len(all_trades):,}  pour {len(set(t['condition_id'] for t in all_trades))} marchés")

if len(all_trades) == 0:
    print("  ⚠  Aucun trade récupéré. Vérifier le format de l'API data-api.polymarket.com/trades")
    import sys; sys.exit(1)

df_trades = pd.DataFrame(all_trades)


# ── Étape 4 : Analyse par fenêtre temporelle ─────────────────────────────────

print("\n[4/4] Calcul ROI par fenêtre temporelle...")

results = []

for window_sec in ALL_WINDOWS:
    # Dernier trade connu AVANT ce moment pour chaque marché
    window_df = df_trades[df_trades["secs_before"] >= window_sec].copy()
    if len(window_df) < 5:
        continue

    # Pour chaque marché : garder le trade le plus récent (secs_before le plus petit)
    last = (
        window_df
        .sort_values("secs_before")  # du plus proche au plus loin de la fin
        .groupby("condition_id")
        .first()                      # le plus récent = secs_before minimal
        [["price_yes", "yes_won"]]
        .reset_index()
    )

    if len(last) < 5:
        continue

    p_yes   = last["price_yes"].values.astype(float)
    yes_won = last["yes_won"].values.astype(float)

    # Upsider = side avec prix > 0.5
    yes_is_upsider = p_yes > 0.5

    # Victoires
    upsider_wins  = np.where(yes_is_upsider, yes_won == 1, yes_won == 0)
    underdog_wins = ~upsider_wins

    # Prix des deux sides
    p_upsider  = np.clip(np.where(yes_is_upsider, p_yes, 1.0 - p_yes), 0.01, 0.99)
    p_underdog = np.clip(1.0 - p_upsider, 0.01, 0.99)

    # ROI par trade : win_rate*(1/P - 1) - (1 - win_rate)
    roi_up_per  = np.where(upsider_wins,  1.0/p_upsider  - 1.0, -1.0)
    roi_dog_per = np.where(underdog_wins, 1.0/p_underdog - 1.0, -1.0)

    results.append({
        "secs_before":      window_sec,
        "n_markets":        len(last),
        "p_yes_mean":       p_yes.mean(),
        "p_yes_std":        p_yes.std(),
        "wr_upsider":       upsider_wins.mean(),
        "wr_underdog":      underdog_wins.mean(),
        "roi_upsider":      roi_up_per.mean(),
        "roi_underdog":     roi_dog_per.mean(),
        "roi_upsider_pct":  roi_up_per.mean()  * 100,
        "roi_underdog_pct": roi_dog_per.mean() * 100,
    })

df = pd.DataFrame(results).sort_values("secs_before", ascending=False)

n_markets_usable = df_trades["condition_id"].nunique()


# ── Tableau bilan ─────────────────────────────────────────────────────────────

def secs_to_label(s):
    s = int(s)
    return f"{s//60}m{s%60:02d}s" if s >= 60 else f"    {s}s"

print("\n" + "=" * 106)
print("  BILAN — HEDGE SUR MARCHÉS BITCOIN UP/DOWN 5 MINUTES")
print("=" * 106)
print(f"\n  Marchés analysés : {n_markets_usable}  |  Fenêtres : {len(df)}\n")

print(f"  {'Temps avant':>12}  {'N':>5}  {'P_yes':>6}  "
      f"{'WR upsider':>10}  {'ROI upsider':>11}  "
      f"{'WR underdog':>11}  {'ROI underdog':>12}")
print("  " + "-" * 100)

for _, row in df.iterrows():
    up_ok  = "✓" if row["roi_upsider"]  > 0 else " "
    dog_ok = "✓" if row["roi_underdog"] > 0 else " "
    print(
        f"  {secs_to_label(row['secs_before']):>12}  "
        f"{int(row['n_markets']):>5}  "
        f"{row['p_yes_mean']:>5.1%}  "
        f"  {row['wr_upsider']:>8.1%}  "
        f"  {up_ok}{row['roi_upsider_pct']:>9.1f}%  "
        f"  {row['wr_underdog']:>9.1%}  "
        f"  {dog_ok}{row['roi_underdog_pct']:>9.1f}%"
    )

# ── Interprétation ────────────────────────────────────────────────────────────

print("\n" + "=" * 106)
print("  INTERPRÉTATION")
print("=" * 106)

best_up  = df.loc[df["roi_upsider"].idxmax()]
best_dog = df.loc[df["roi_underdog"].idxmax()]

print(f"\n  Meilleur ROI upsider  : {best_up['roi_upsider_pct']:+.1f}% "
      f"à {secs_to_label(best_up['secs_before'])} avant résolution "
      f"(WR={best_up['wr_upsider']:.1%}, N={int(best_up['n_markets'])})")

print(f"  Meilleur ROI underdog : {best_dog['roi_underdog_pct']:+.1f}% "
      f"à {secs_to_label(best_dog['secs_before'])} avant résolution "
      f"(WR={best_dog['wr_underdog']:.1%}, N={int(best_dog['n_markets'])})")

for label, col, wr_col in [("upsider", "roi_upsider", "wr_upsider"),
                             ("underdog", "roi_underdog", "wr_underdog")]:
    positives = df[df[col] > 0.02]
    print(f"\n  Fenêtres avec ROI {label} > 2% : {len(positives)}")
    for _, r in positives.iterrows():
        print(f"    → {secs_to_label(r['secs_before']):<8}  "
              f"ROI={r[col+'_pct']:+.1f}%  WR={r[wr_col]:.1%}  N={int(r['n_markets'])}")

# ── Sauvegarde ────────────────────────────────────────────────────────────────

csv_path = OUTPUT_DIR / "btc_5min_hedge_results.csv"
df.to_csv(csv_path, index=False)
print(f"\n  CSV sauvegardé : {csv_path}")

# ── Graphiques ────────────────────────────────────────────────────────────────

fig, axes = plt.subplots(2, 2, figsize=(16, 10))
fig.suptitle(f"Marchés Bitcoin up/down ~5min — {n_markets_usable} marchés analysés",
             fontsize=14, fontweight="bold")

x = df["secs_before"].values

ax = axes[0, 0]
ax.plot(x, df["wr_upsider"]  * 100, "b-o", ms=4, label="Upsider (majoritaire)")
ax.plot(x, df["wr_underdog"] * 100, "r-o", ms=4, label="Underdog (minoritaire)")
ax.axhline(50, color="gray", ls="--", alpha=0.5, label="50%")
ax.set_xlabel("Secondes avant résolution"); ax.set_ylabel("Win rate (%)")
ax.set_title("Win rate"); ax.legend(); ax.set_xlim(max(x), 0)
ax.grid(True, alpha=0.3); ax.yaxis.set_major_formatter(mtick.PercentFormatter())

ax = axes[0, 1]
ax.plot(x, df["roi_upsider_pct"],  "b-o", ms=4, label="Upsider")
ax.plot(x, df["roi_underdog_pct"], "r-o", ms=4, label="Underdog")
ax.axhline(0, color="black", lw=1.5)
ax.fill_between(x, df["roi_upsider_pct"],  0, where=(df["roi_upsider_pct"]  > 0), alpha=0.15, color="blue")
ax.fill_between(x, df["roi_underdog_pct"], 0, where=(df["roi_underdog_pct"] > 0), alpha=0.15, color="red")
ax.set_xlabel("Secondes avant résolution"); ax.set_ylabel("ROI moyen (%)")
ax.set_title("ROI moyen"); ax.legend(); ax.set_xlim(max(x), 0)
ax.grid(True, alpha=0.3); ax.yaxis.set_major_formatter(mtick.PercentFormatter())

ax = axes[1, 0]
ax.plot(x, df["p_yes_mean"] * 100, "g-o", ms=4)
ax.fill_between(x,
                (df["p_yes_mean"] - df["p_yes_std"]) * 100,
                (df["p_yes_mean"] + df["p_yes_std"]) * 100,
                alpha=0.15, color="green", label="±1σ")
ax.axhline(50, color="gray", ls="--", alpha=0.5)
ax.set_xlabel("Secondes avant résolution"); ax.set_ylabel("Prix YES moyen (%)")
ax.set_title("Prix YES moyen dans les 5 dernières minutes")
ax.legend(); ax.set_xlim(max(x), 0); ax.grid(True, alpha=0.3)
ax.yaxis.set_major_formatter(mtick.PercentFormatter())

ax = axes[1, 1]
ax.bar(range(len(x)), df["n_markets"].values, color="steelblue", alpha=0.7)
ax.set_xticks(range(len(x)))
ax.set_xticklabels([secs_to_label(s) for s in x], rotation=90, fontsize=7)
ax.set_xlabel("Fenêtre temporelle"); ax.set_ylabel("Nombre de marchés")
ax.set_title("Marchés avec données par fenêtre"); ax.grid(True, alpha=0.3, axis="y")

plt.tight_layout()
png_path = OUTPUT_DIR / "btc_5min_hedge_chart.png"
plt.savefig(png_path, dpi=150, bbox_inches="tight")
print(f"  PNG sauvegardé : {png_path}")
plt.close()

print("\nAnalyse terminée.\n")
