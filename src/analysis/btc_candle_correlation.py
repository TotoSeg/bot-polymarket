"""
Correlation entre bougies 5m BTC consecutives
==============================================
Telecharge toutes les bougies BTC/USDT 5m depuis Binance (2017 -> aujourd'hui)
et teste : la direction d'une bougie predit-elle la suivante ?

Analyse :
  - P(next_up | prev_up) vs P(next_up | prev_down)
  - Chi2 test d'independance
  - Autocorrelation sur plusieurs lags (1 a 10 bougies)
  - Momentum vs mean-reversion selon la magnitude du mouvement

Sortie : data/updown/btc_5m_candles.parquet
"""

import sys, time, json
from pathlib import Path
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
from scipy import stats

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BINANCE_API = "https://api.binance.com/api/v3/klines"
SYMBOL      = "BTCUSDT"
INTERVAL    = "5m"
LIMIT       = 1000          # max par requete Binance
DELAY       = 0.08          # secondes entre requetes
OUT_PATH    = Path("data/updown/btc_5m_candles.parquet")

# Debut : 17 aout 2017 (1er jour du BTCUSDT sur Binance)
START_MS    = int(datetime(2017, 8, 17, tzinfo=timezone.utc).timestamp() * 1000)


def fetch_all_candles() -> pd.DataFrame:
    """Telecharge toutes les bougies 5m depuis Binance."""
    all_rows = []
    start_ms = START_MS
    total    = 0

    print(f"Telechargement BTC/USDT 5m depuis Binance (2017 -> aujourd'hui)...")

    while True:
        try:
            resp = requests.get(BINANCE_API, params={
                "symbol":    SYMBOL,
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

        all_rows.extend(data)
        total += len(data)
        last_ts = data[-1][0]
        last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

        if total % 50_000 < LIMIT:
            print(f"  {total:>8,} bougies | jusqu'au {last_dt}")

        if len(data) < LIMIT:
            break

        start_ms = last_ts + 1
        time.sleep(DELAY)

    print(f"  Total : {total:,} bougies")

    # Colonnes Binance klines
    cols = ["open_time","open","high","low","close","volume",
            "close_time","quote_vol","nb_trades","taker_base","taker_quote","ignore"]
    df = pd.DataFrame(all_rows, columns=cols)
    df = df[["open_time","open","high","low","close","volume","nb_trades"]].copy()
    for c in ["open","high","low","close","volume"]:
        df[c] = df[c].astype(float)
    df["nb_trades"] = df["nb_trades"].astype(int)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.sort_values("open_time").reset_index(drop=True)
    return df


def analyze(df: pd.DataFrame):
    """Analyse la correlation entre bougies consecutives."""

    # Direction : 1=Up (close>open), 0=Down
    df["dir"] = (df["close"] > df["open"]).astype(int)
    df["ret"] = (df["close"] - df["open"]) / df["open"]   # rendement %

    n = len(df)
    print(f"\nPeriode : {df['open_time'].iloc[0].date()} -> {df['open_time'].iloc[-1].date()}")
    print(f"Nb bougies : {n:,}")
    print(f"% Up : {df['dir'].mean()*100:.2f}%")

    # ── 1. Correlation brute ─────────────────────────────────────────────────
    print("\n" + "=" * 55)
    print("1. CORRELATION BRUTE (bougie N -> bougie N+1)")
    print("=" * 55)

    prev_up   = df["dir"].iloc[:-1].values
    next_dir  = df["dir"].iloc[1:].values

    p_next_up_given_prev_up   = next_dir[prev_up == 1].mean()
    p_next_up_given_prev_down = next_dir[prev_up == 0].mean()
    p_next_up_overall         = next_dir.mean()

    print(f"P(next=Up | prev=Up)   : {p_next_up_given_prev_up*100:.3f}%")
    print(f"P(next=Up | prev=Down) : {p_next_up_given_prev_down*100:.3f}%")
    print(f"P(next=Up) overall     : {p_next_up_overall*100:.3f}%")
    print(f"Difference             : {(p_next_up_given_prev_up - p_next_up_given_prev_down)*100:+.3f}pp")

    # Test chi2
    ct = pd.crosstab(prev_up, next_dir)
    chi2, p_val, _, _ = stats.chi2_contingency(ct)
    print(f"\nChi2 = {chi2:.2f}  |  p-value = {p_val:.2e}")
    print("-> " + ("CORRELATION SIGNIFICATIVE" if p_val < 0.01 else "pas de correlation significative"))

    # ── 2. Autocorrelation sur plusieurs lags ────────────────────────────────
    print("\n" + "=" * 55)
    print("2. AUTOCORRELATION SUR PLUSIEURS LAGS")
    print("=" * 55)
    print(f"{'Lag':>5}  {'P(next=Up|prev=Up)':>20}  {'P(next=Up|prev=Down)':>22}  {'diff':>8}  {'p-val':>10}")
    for lag in [1, 2, 3, 5, 10, 20]:
        prev = df["dir"].iloc[:-lag].values
        nxt  = df["dir"].iloc[lag:].values
        pu   = nxt[prev == 1].mean()
        pd_  = nxt[prev == 0].mean()
        ct_  = pd.crosstab(prev, nxt)
        _, p_, _, _ = stats.chi2_contingency(ct_)
        sig = "***" if p_ < 0.001 else "**" if p_ < 0.01 else "*" if p_ < 0.05 else ""
        print(f"{lag:>5}  {pu*100:>19.3f}%  {pd_*100:>21.3f}%  {(pu-pd_)*100:>+7.3f}pp  {p_:>10.2e} {sig}")

    # ── 3. Momentum selon la magnitude ──────────────────────────────────────
    print("\n" + "=" * 55)
    print("3. CORRELATION SELON LA MAGNITUDE DU MOUVEMENT PRECEDENT")
    print("=" * 55)

    df2 = df.copy()
    df2["next_dir"] = df2["dir"].shift(-1)
    df2 = df2.dropna(subset=["next_dir"])
    df2["next_dir"] = df2["next_dir"].astype(int)

    # Quartiles du rendement absolu
    df2["abs_ret_pct"] = df2["ret"].abs() * 100
    bins = [0, 0.05, 0.15, 0.30, 0.60, np.inf]
    labels = ["<0.05%", "0.05-0.15%", "0.15-0.30%", "0.30-0.60%", ">0.60%"]
    df2["mag_bucket"] = pd.cut(df2["abs_ret_pct"], bins=bins, labels=labels)

    for bucket in labels:
        sub = df2[df2["mag_bucket"] == bucket]
        if len(sub) < 100:
            continue
        sub_up   = sub[sub["dir"] == 1]["next_dir"].mean()
        sub_down = sub[sub["dir"] == 0]["next_dir"].mean()
        diff = sub_up - sub_down
        print(f"  {bucket:>12s} | n={len(sub):>7,} | P(next_up|up)={sub_up*100:.2f}% | "
              f"P(next_up|down)={sub_down*100:.2f}% | diff={diff*100:+.2f}pp")

    # ── 4. Conclusion EV pour une strategie Polymarket ──────────────────────
    print("\n" + "=" * 55)
    print("4. IMPLICATION POUR STRATEGIE POLYMARKET")
    print("=" * 55)

    # Si on bet 'Up' apres une bougie Up, quel est notre edge vs le marche ?
    # Le marche price 'Up' a ~50c (marche equilibre)
    # Notre prediction = P(next_up | prev_up)
    fee = 0.02
    for threshold, label in [(0.50, "neutre (50%)"), (0.51, "+1pp"), (0.52, "+2pp")]:
        if p_next_up_given_prev_up > threshold:
            gain = (1 - 0.5) / 0.5 * (1 - fee)   # achat a 50c
            ev = p_next_up_given_prev_up * gain - (1 - p_next_up_given_prev_up)
            print(f"  Si achat 'Up' a 50c apres bougie Up : EV = {ev*100:+.3f}%  "
                  f"(WR={p_next_up_given_prev_up*100:.2f}%)")
            break
    else:
        print("  Pas d'edge detecte (correlation insuffisante)")


def main():
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if OUT_PATH.exists():
        print(f"Chargement depuis cache : {OUT_PATH}")
        df = pd.read_parquet(OUT_PATH)
        print(f"  {len(df):,} bougies chargees")
    else:
        df = fetch_all_candles()
        df.to_parquet(OUT_PATH, index=False, compression="zstd")
        print(f"Sauve : {OUT_PATH} ({OUT_PATH.stat().st_size/1024/1024:.1f} MB)")

    analyze(df)


if __name__ == "__main__":
    main()
