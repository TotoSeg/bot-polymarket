"""
Analyse multi-signal bougies 5m - tous assets (ETH, SOL, XRP, DOGE + BTC deja dispo)
=======================================================================================
1. Telecharge les candles depuis Binance pour chaque asset
2. Applique la meme analyse streak+magnitude que btc_candle_multi_signal.py
3. Produit un tableau comparatif pour identifier les meilleurs signaux par asset
"""
import sys, time
from pathlib import Path
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BINANCE_API = "https://api.binance.com/api/v3/klines"
INTERVAL    = "5m"
LIMIT       = 1000
DELAY       = 0.08

# Assets a analyser (BTC deja telecharge)
START_2024 = datetime(2024, 1, 1, tzinfo=timezone.utc)

ASSETS = {
    "ETH":  {"symbol": "ETHUSDT",  "start": START_2024},
    "SOL":  {"symbol": "SOLUSDT",  "start": START_2024},
    "XRP":  {"symbol": "XRPUSDT",  "start": START_2024},
    "DOGE": {"symbol": "DOGEUSDT", "start": START_2024},
}

DATA_DIR = Path("data/updown")


# ==============================================================
# TELECHARGEMENT
# ==============================================================

COLS = ["open_time","open","high","low","close","volume",
        "close_time","quote_vol","n_trades","taker_buy_base",
        "taker_buy_quote","ignore"]
CHECKPOINT_INTERVAL = 50_000  # sauvegarde intermediaire toutes les N bougies


def rows_to_df(rows: list) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLS)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    return df


def fetch_candles(symbol: str, start_dt: datetime, out_path: Path) -> pd.DataFrame:
    """Telecharge toutes les bougies 5m depuis Binance avec reprise automatique."""
    tmp_path = out_path.with_suffix(".tmp.parquet")

    # Fichier final deja complet -> charger directement
    if out_path.exists():
        print(f"  Deja telecharge : {out_path} — chargement...")
        return pd.read_parquet(out_path)

    # Fichier temporaire = download interrompu -> reprendre depuis la fin
    all_rows = []
    if tmp_path.exists():
        df_tmp  = pd.read_parquet(tmp_path)
        last_ts = int(df_tmp["open_time"].max().timestamp() * 1000)
        start_ms = last_ts + 5 * 60 * 1000  # +1 bougie de 5min
        all_rows = df_tmp.values.tolist()  # pas utilise pour la sauvegarde intermediaire
        print(f"  Reprise depuis checkpoint : {len(df_tmp):,} bougies deja sauvegardees")
        # On travaille directement avec le dataframe existant pour le checkpoint
        existing_df = df_tmp
    else:
        start_ms    = int(start_dt.timestamp() * 1000)
        existing_df = None

    new_rows = []
    total    = len(existing_df) if existing_df is not None else 0

    print(f"  Telechargement {symbol} 5m depuis Binance...")
    while True:
        try:
            resp = requests.get(BINANCE_API, params={
                "symbol":    symbol,
                "interval":  INTERVAL,
                "startTime": start_ms,
                "limit":     LIMIT,
            }, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  Erreur : {e} — retry dans 3s")
            time.sleep(3)
            continue

        if not data:
            break

        new_rows.extend(data)
        total    += len(data)
        start_ms  = data[-1][6] + 1  # close_time + 1ms

        # Checkpoint periodique
        if len(new_rows) >= CHECKPOINT_INTERVAL:
            df_new  = rows_to_df(new_rows)
            parts   = [p for p in [existing_df, df_new] if p is not None]
            df_save = pd.concat(parts, ignore_index=True)
            df_save.to_parquet(tmp_path, index=False)
            existing_df = df_save
            new_rows    = []
            print(f"    {total:>7,} bougies (checkpoint sauvegarde)")

        if len(data) < LIMIT:
            break

        time.sleep(DELAY)

    # Fusion finale
    df_new  = rows_to_df(new_rows) if new_rows else pd.DataFrame(columns=COLS)
    parts   = [p for p in [existing_df, df_new] if p is not None and len(p) > 0]
    df      = pd.concat(parts, ignore_index=True).drop_duplicates(subset=["open_time"])
    df      = df.sort_values("open_time").reset_index(drop=True)

    df.to_parquet(out_path, index=False)
    if tmp_path.exists():
        tmp_path.unlink()  # supprimer le fichier temporaire
    print(f"  Sauvegarde -> {out_path} ({len(df):,} bougies)")
    return df


# ==============================================================
# ANALYSE SIGNAUX
# ==============================================================

def compute_thresholds(df: pd.DataFrame, asset: str,
                        recent_days: int = 90) -> dict:
    """
    Calcule les seuils de magnitude adaptatifs pour un asset.

    Utilise les percentiles de la distribution des |rendements| sur :
      - toute la periode (stabilite statistique)
      - les 90 derniers jours (conditions actuelles du marche)

    Retourne un dict avec les seuils p50, p75, p90 sur les deux periodes,
    plus le seuil retenu (moyenne ponderee 40% global / 60% recent).
    """
    ret_abs = ((df["close"] - df["open"]) / df["open"] * 100).abs()
    ret_abs = ret_abs[ret_abs > 0]  # exclure les bougies plates

    cutoff = df["open_time"].max() - pd.Timedelta(days=recent_days)
    ret_recent = ret_abs[df["open_time"] >= cutoff]

    thresholds = {}
    for pct in [50, 75, 90]:
        full_val   = ret_abs.quantile(pct / 100)
        recent_val = ret_recent.quantile(pct / 100) if len(ret_recent) > 100 else full_val
        # Poids : 40% historique complet, 60% recent (conditions actuelles)
        blended    = 0.40 * full_val + 0.60 * recent_val
        thresholds[f"p{pct}"] = round(blended, 4)

    print(f"  Seuils adaptatifs {asset} (40% global / 60% 90j) :")
    print(f"    p50={thresholds['p50']:.4f}%  p75={thresholds['p75']:.4f}%  "
          f"p90={thresholds['p90']:.4f}%")
    return thresholds


def analyze_signals(df: pd.DataFrame, asset: str) -> pd.DataFrame:
    """Calcule les signaux streak+magnitude avec seuils adaptatifs par asset."""
    df = df.copy().sort_values("open_time").reset_index(drop=True)
    df["ret"]     = (df["close"] - df["open"]) / df["open"] * 100
    df["dir"]     = (df["close"] > df["open"]).astype(int)
    df["ret_abs"] = df["ret"].abs()

    for lag in [1, 2, 3, 4]:
        df[f"dir_lag{lag}"] = df["dir"].shift(lag)
        df[f"abs_lag{lag}"] = df["ret_abs"].shift(lag)

    df = df.dropna()
    df = df.astype({f"dir_lag{i}": int for i in range(1, 5)})
    N  = len(df)

    # Seuils adaptatifs bases sur la distribution reelle de l'asset
    thr = compute_thresholds(df, asset)
    p50, p75, p90 = thr["p50"], thr["p75"], thr["p90"]

    mag_filters = [
        ("any",   0,    np.inf),
        (">p50",  p50,  np.inf),   # bougie "normale" (50% les plus grandes)
        (">p75",  p75,  np.inf),   # bougie "forte"   (top 25%)
        (">p90",  p90,  np.inf),   # bougie "extreme" (top 10%)
        # Zone mediane : entre p50 et p75 (bougies moderees)
        ("p50-p75", p50, p75),
    ]

    results = []

    # --- Streaks seuls ---
    for streak_len in [1, 2, 3, 4]:
        for dir_val, dir_label in [(1, "UP"), (0, "DOWN")]:
            mask = pd.Series([True] * N, index=df.index)
            for lag in range(1, streak_len + 1):
                mask = mask & (df[f"dir_lag{lag}"] == dir_val)
            sub = df[mask]
            if len(sub) < 50:
                continue
            p_rev = (1 - sub["dir"].mean()) if dir_val == 1 else sub["dir"].mean()
            results.append({
                "asset": asset, "signal": f"{dir_label}x{streak_len}",
                "mag_filter": "any",
                "mag_threshold_pct": 0.0,
                "n": len(sub),
                "p_mean_rev": p_rev, "ev_pp": (p_rev - 0.5) * 100,
                "opp_per_day": len(sub) / (N / 288),
            })

    # --- Magnitude + streak (seuils adaptatifs) ---
    for streak_len in [1, 2, 3]:
        for dir_val, dir_label in [(1, "UP"), (0, "DOWN")]:
            for mag_label, lo, hi in mag_filters:
                if mag_label == "any":
                    continue
                mask = (df["dir_lag1"] == dir_val) & (df["abs_lag1"].between(lo, hi))
                for lag in range(2, streak_len + 1):
                    mask = mask & (df[f"dir_lag{lag}"] == dir_val)
                sub = df[mask]
                if len(sub) < 50:
                    continue
                p_rev = (1 - sub["dir"].mean()) if dir_val == 1 else sub["dir"].mean()
                results.append({
                    "asset": asset, "signal": f"{dir_label}x{streak_len}",
                    "mag_filter": mag_label,
                    "mag_threshold_pct": round(lo, 4),
                    "n": len(sub),
                    "p_mean_rev": p_rev, "ev_pp": (p_rev - 0.5) * 100,
                    "opp_per_day": len(sub) / (N / 288),
                })

    return pd.DataFrame(results)


# ==============================================================
# NIVEAUX PSYCHOLOGIQUES PAR ASSET
# ==============================================================

ROUND_LEVELS = {
    "BTC":  [1_000, 5_000, 10_000],   # ex: 70k, 75k, 80k...
    "ETH":  [100, 500],
    "SOL":  [10, 50, 100],
    "XRP":  [0.10, 0.50, 1.00],
    "DOGE": [0.01, 0.05, 0.10],
}

def get_levels_in_range(asset: str, prices: pd.Series) -> list:
    """Retourne tous les niveaux ronds dans la plage de prix de l'asset."""
    steps   = ROUND_LEVELS.get(asset, [])
    lo, hi  = prices.min(), prices.max()
    levels  = []
    for step in steps:
        n_lo = int(lo / step)
        n_hi = int(hi / step) + 2
        levels += [round(i * step, 10) for i in range(n_lo, n_hi)]
    # Garder uniquement les niveaux dans la plage + 10% de marge
    levels = [l for l in levels if lo * 0.9 <= l <= hi * 1.1 and l > 0]
    return sorted(set(levels))


def analyze_price_levels(df_raw: pd.DataFrame, asset: str) -> None:
    """
    Analyse le comportement des bougies autour des niveaux ronds.

    Evenements detectes :
      - CROSS_DOWN : open > niveau, close < niveau  -> test retour vers le haut ?
      - CROSS_UP   : open < niveau, close > niveau  -> test retour vers le bas ?
      - BOUNCE     : low < niveau, close > niveau   -> support tenu, continuation UP ?
      - REJECT     : high > niveau, close < niveau  -> resistance rejetee, continuation DOWN ?
      - PROXIMITY  : |close - niveau| / niveau < 0.3% -> biais directionnel ?
    """
    df = df_raw.copy().sort_values("open_time").reset_index(drop=True)
    df["ret"] = (df["close"] - df["open"]) / df["open"] * 100
    df["dir"] = (df["close"] > df["open"]).astype(int)
    df["dir_next"] = df["dir"].shift(-1)   # direction de la bougie SUIVANTE
    df = df.dropna(subset=["dir_next"])
    df["dir_next"] = df["dir_next"].astype(int)

    levels = get_levels_in_range(asset, df["close"])
    if not levels:
        return

    print(f"\n  {asset} — {len(levels)} niveaux ronds analyses "
          f"(min={min(levels):.4g}, max={max(levels):.4g})")
    print(f"  {'Evenement':<14} {'Niveau ex.':<12} {'n':>6} {'P(prevu)':>10} {'EV(pp)':>8} {'Opp/j':>7}")
    print("  " + "-" * 60)

    N = len(df)
    days = N / 288.0
    summary = []

    for event_label, mask_fn, expected_dir in [
        # CROSS_DOWN : bougie franchit le niveau par le bas -> on attend rebond UP
        ("CROSS_DOWN",
         lambda d, lvl: (d["open"] > lvl) & (d["close"] < lvl),
         1),
        # CROSS_UP : bougie franchit le niveau par le haut -> on attend rechute DOWN
        ("CROSS_UP",
         lambda d, lvl: (d["open"] < lvl) & (d["close"] > lvl),
         0),
        # BOUNCE : mèche en dessous mais close au-dessus -> support tenu, UP attendu
        ("BOUNCE_SUP",
         lambda d, lvl: (d["low"] < lvl) & (d["close"] > lvl) & (d["open"] > lvl),
         1),
        # REJECT : mèche au-dessus mais close en dessous -> résistance, DOWN attendu
        ("REJECT_RES",
         lambda d, lvl: (d["high"] > lvl) & (d["close"] < lvl) & (d["open"] < lvl),
         0),
    ]:
        masks_combined = pd.Series([False] * N, index=df.index)
        for lvl in levels:
            masks_combined = masks_combined | mask_fn(df, lvl)

        sub = df[masks_combined]
        if len(sub) < 20:
            continue

        p_correct = (sub["dir_next"] == expected_dir).mean()
        ev = (p_correct - 0.5) * 100
        opp_per_day = len(sub) / days
        # Exemple de niveau le plus frequent
        best_lvl = None
        best_n   = 0
        for lvl in levels:
            n_lvl = mask_fn(df, lvl).sum()
            if n_lvl > best_n:
                best_n, best_lvl = n_lvl, lvl

        summary.append((event_label, best_lvl, len(sub), p_correct, ev, opp_per_day))
        marker = " <-- EDGE" if abs(ev) > 3 else ""
        print(f"  {event_label:<14} {best_lvl:<12.4g} {len(sub):>6,} "
              f"{p_correct*100:>9.2f}% {ev:>+7.2f}pp {opp_per_day:>6.1f}{marker}")

    # PROXIMITY : prix proche d'un niveau -> biais selon direction de la bougie actuelle
    print(f"\n  PROXIMITY (<0.3% du niveau) :")
    for prox_pct in [0.30, 0.15]:
        mask_prox = pd.Series([False] * N, index=df.index)
        for lvl in levels:
            mask_prox = mask_prox | (((df["close"] - lvl).abs() / lvl) < prox_pct / 100)

        sub = df[mask_prox]
        if len(sub) < 20:
            continue
        # Quand le prix est proche et la bougie est UP -> mean-rev (achat DOWN) ?
        for cur_dir, cur_lbl, exp in [(1, "proche+UP", 0), (0, "proche+DOWN", 1)]:
            s2 = sub[sub["dir"] == cur_dir]
            if len(s2) < 10:
                continue
            p  = (s2["dir_next"] == exp).mean()
            ev = (p - 0.5) * 100
            print(f"    <{prox_pct:.2f}% du niveau, bougie {cur_lbl:<12} | "
                  f"n={len(s2):>5,} | P={p*100:.2f}% | EV={ev:>+.2f}pp")

    # COMBINED : niveau + streak (signal le plus fort)
    print(f"\n  COMBINED niveau + streak :")
    df["dir_lag1"] = df["dir"].shift(1)
    df2 = df.dropna(subset=["dir_lag1"])
    df2["dir_lag1"] = df2["dir_lag1"].astype(int)

    for event_label, mask_fn, streak_dir, expected_dir in [
        ("CROSS_DOWN+streak_UP",
         lambda d, lvl: (d["open"] > lvl) & (d["close"] < lvl),
         1, 1),
        ("CROSS_UP+streak_DOWN",
         lambda d, lvl: (d["open"] < lvl) & (d["close"] > lvl),
         0, 0),
    ]:
        mask_level  = pd.Series([False] * len(df2), index=df2.index)
        for lvl in levels:
            mask_level = mask_level | mask_fn(df2, lvl)
        mask_streak = df2["dir_lag1"] == streak_dir
        sub = df2[mask_level & mask_streak]
        if len(sub) < 10:
            continue
        p  = (sub["dir_next"] == expected_dir).mean()
        ev = (p - 0.5) * 100
        print(f"    {event_label:<30} | n={len(sub):>4,} | P={p*100:.2f}% | EV={ev:>+.2f}pp")


# ==============================================================
# ANALYSE HEURE DE LA JOURNEE
# ==============================================================

def analyze_time_of_day(df_raw: pd.DataFrame, asset: str) -> None:
    """
    Mean-reversion varie-t-elle selon l'heure UTC ?
    Sessions : Asie (0-8h), Europe (8-16h), US (13-21h)
    """
    df = df_raw.copy().sort_values("open_time").reset_index(drop=True)
    df["dir"]     = (df["close"] > df["open"]).astype(int)
    df["dir_lag1"] = df["dir"].shift(1)
    df["abs_lag1"] = ((df["close"] - df["open"]) / df["open"] * 100).abs().shift(1)
    df = df.dropna()
    df["dir_lag1"] = df["dir_lag1"].astype(int)
    df["hour"]    = df["open_time"].dt.hour

    # Meilleur signal general : streak UP x1 fort (>0.20%)
    mask_signal = (df["dir_lag1"] == 1) & (df["abs_lag1"] > 0.20)

    print(f"\n  {asset} — Mean-reversion par session (signal: UP fort >0.20%)")
    print(f"  {'Session':<20} {'Heures UTC':<14} {'n':>6} {'P(DOWN)':>9} {'EV(pp)':>8}")
    print("  " + "-" * 60)

    sessions = [
        ("Asie",    range(0,  8)),
        ("Europe",  range(8,  16)),
        ("US",      range(13, 22)),
        ("Nuit US", list(range(21, 24)) + list(range(0, 5))),
    ]
    for sess_name, hours in sessions:
        mask_hour = df["hour"].isin(hours)
        sub = df[mask_signal & mask_hour]
        if len(sub) < 30:
            continue
        p   = 1 - sub["dir"].mean()   # P(next = DOWN) apres UP
        ev  = (p - 0.5) * 100
        h_range = f"{min(hours)}-{max(hours)}h"
        marker = " <--" if ev > 4 else ""
        print(f"  {sess_name:<20} {h_range:<14} {len(sub):>6,} {p*100:>8.2f}% {ev:>+7.2f}pp{marker}")


# ==============================================================
# MAIN
# ==============================================================

all_results = []

# BTC deja telecharge
print("=" * 60)
print("BTC (deja disponible)")
print("=" * 60)
btc_path = DATA_DIR / "btc_5m_candles.parquet"
df_btc   = pd.read_parquet(btc_path)
df_btc   = df_btc[df_btc["open_time"] >= pd.Timestamp("2024-01-01", tz="UTC")]
res_btc  = analyze_signals(df_btc, "BTC")
all_results.append(res_btc)
print(f"  {len(df_btc):,} bougies analysees")

# Tous les assets dans un dict pour l'analyse prix
all_dfs = {"BTC": df_btc}

# Autres assets
for asset, cfg in ASSETS.items():
    print()
    print("=" * 60)
    print(f"{asset}")
    print("=" * 60)
    out_path = DATA_DIR / f"{asset.lower()}_5m_candles.parquet"
    df_asset = fetch_candles(cfg["symbol"], cfg["start"], out_path)
    df_asset = df_asset[df_asset["open_time"] >= pd.Timestamp("2024-01-01", tz="UTC")]
    res      = analyze_signals(df_asset, asset)
    all_results.append(res)
    all_dfs[asset] = df_asset
    print(f"  {len(df_asset):,} bougies analysees")

# ==============================================================
# TABLEAU COMPARATIF
# ==============================================================

df_all = pd.concat(all_results, ignore_index=True)

print()
print("=" * 70)
print("TABLEAU COMPARATIF - MEILLEURS SIGNAUX PAR ASSET")
print("=" * 70)

# Top 5 par asset
for asset in ["BTC", "ETH", "SOL", "XRP", "DOGE"]:
    sub = df_all[df_all["asset"] == asset].sort_values("ev_pp", ascending=False).head(5)
    print(f"\n  {asset}:")
    print(f"  {'Signal':<12} {'Mag':>10} {'n':>7} {'P(rev)':>8} {'EV(pp)':>8} {'Opp/j':>7}")
    print("  " + "-" * 55)
    for _, row in sub.iterrows():
        print(f"  {row['signal']:<12} {row['mag_filter']:>10} {row['n']:>7,} "
              f"{row['p_mean_rev']*100:>7.2f}% {row['ev_pp']:>+7.2f}pp {row['opp_per_day']:>6.1f}")

print()
print("=" * 70)
print("SIGNAUX COMMUNS - MEILLEURE ENTREE UNIVERSELLE")
print("=" * 70)
print("(Signaux avec EV > 5pp sur TOUS les assets)")
print()

# Pivot : pour chaque (signal, mag_filter), EV par asset
pivot = df_all.pivot_table(
    index=["signal", "mag_filter"],
    columns="asset",
    values="ev_pp",
    aggfunc="mean"
)
pivot = pivot.dropna()  # garder uniquement les lignes ou tous les assets sont presents
pivot["min_ev"] = pivot.min(axis=1)
pivot = pivot.sort_values("min_ev", ascending=False)

threshold_common = 3.0
filtered = pivot[pivot["min_ev"] > threshold_common]
if filtered.empty:
    threshold_common = pivot["min_ev"].quantile(0.75)
    filtered = pivot[pivot["min_ev"] > threshold_common]
    print(f"(Seuil abaisse au 3e quartile : >{threshold_common:.2f}pp)\n")
print(filtered.to_string())

print()
print("=" * 70)
print("SYNTHESE OPERATIONNELLE")
print("=" * 70)

# Le meilleur signal universel
best_universal = pivot[pivot["min_ev"] > threshold_common].head(3)
if len(best_universal) > 0:
    for (sig, mag), row in best_universal.iterrows():
        print(f"\n  Signal : {sig}, N-1 {mag}")
        for a in ["BTC","ETH","SOL","XRP","DOGE"]:
            if a in row:
                opp = df_all[(df_all["asset"]==a) &
                             (df_all["signal"]==sig) &
                             (df_all["mag_filter"]==mag)]["opp_per_day"].values
                print(f"    {a}: EV={row[a]:+.2f}pp  ~{opp[0]:.1f} signaux/jour" if len(opp) else f"    {a}: n/a")

Path("outputs").mkdir(parents=True, exist_ok=True)
# Sauvegarder les resultats streaks
df_all.to_csv("outputs/all_assets_candle_signals.csv", index=False)
print(f"\n  Resultats sauvegardes -> outputs/all_assets_candle_signals.csv")

# ==============================================================
# ANALYSE NIVEAUX DE PRIX
# ==============================================================

print()
print("=" * 70)
print("NIVEAUX PSYCHOLOGIQUES (nombres ronds)")
print("=" * 70)
print("Comportement des bougies autour des niveaux ronds (cross/bounce/reject)")

for asset, df_a in all_dfs.items():
    analyze_price_levels(df_a, asset)

# ==============================================================
# ANALYSE HEURE DE LA JOURNEE
# ==============================================================

print()
print("=" * 70)
print("HEURE DE LA JOURNEE — FORCE DU SIGNAL SELON LA SESSION")
print("=" * 70)

for asset, df_a in all_dfs.items():
    analyze_time_of_day(df_a, asset)
