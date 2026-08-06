"""
Analyse multi-signal sur bougies BTC 5m
========================================
Au-dela de la simple bougie N-1, on teste :
  1. Streaks : N bougies consecutives dans la meme direction
  2. Taille relative : la tendance accelere ou decroit ?
  3. Combinaison magnitude + streak
  4. Lags N-1, N-2, N-3 independants et combines
"""
import sys
import pandas as pd
import numpy as np
from scipy.stats import chi2_contingency

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

print("Chargement bougies BTC 5m...")
df = pd.read_parquet("data/updown/btc_5m_candles.parquet")
df["open_ts"] = df["open_time"].astype("int64") // 1000
df["ret"] = (df["close"] - df["open"]) / df["open"] * 100   # rendement %
df["dir"] = (df["close"] > df["open"]).astype(int)           # 1=UP 0=DOWN
df = df.sort_values("open_ts").reset_index(drop=True)
print(f"  {len(df):,} bougies ({df['open_time'].min().date()} -> {df['open_time'].max().date()})")

# Construire les features sur fenetre glissante
df["ret_abs"] = df["ret"].abs()

# Lags (direction et magnitude)
for lag in [1, 2, 3, 4]:
    df[f"dir_lag{lag}"] = df["dir"].shift(lag)
    df[f"ret_lag{lag}"] = df["ret"].shift(lag)
    df[f"abs_lag{lag}"] = df["ret"].abs().shift(lag)

df = df.dropna()
df = df.astype({f"dir_lag{i}": int for i in range(1, 5)})

N = len(df)
print(f"  Bougies avec 4 lags disponibles : {N:,}\n")

# ==============================================================
# 1. ANALYSE PAR STREAK (N bougies consecutives)
# ==============================================================
print("=" * 60)
print("1. STREAKS : N BOUGIES CONSECUTIVES DANS LA MEME DIRECTION")
print("=" * 60)
print(f"{'Streak':>20} | {'n':>7} | {'P(mean-rev)':>11} | {'EV@50c':>8} | {'vs baseline':>11}")
print("-" * 70)

baseline_p = df["dir"].mean()  # P(UP) global

def ev_at_50(p_win, fee=0.02):
    # Achat du cote oppose (ex: DOWN) a 50c -> gain si mean-reversion
    return (p_win * 0.5 / 0.5 - 1) * (1 - fee) if p_win > 0.5 else 0  # simplifie

# Streak de UP -> on attend DOWN
# Streak 1 : juste N-1 UP
for streak_len in [1, 2, 3, 4]:
    # Toutes les N-1..N-streak bougies sont UP
    mask_up = pd.Series([True] * N, index=df.index)
    for lag in range(1, streak_len + 1):
        mask_up = mask_up & (df[f"dir_lag{lag}"] == 1)

    sub = df[mask_up]
    if len(sub) < 50:
        continue
    p_down_next = 1 - sub["dir"].mean()  # P(next = DOWN)
    ev = (p_down_next - (1 - baseline_p)) * 100  # gain vs baseline en pp
    print(f"  UP x{streak_len:>2} -> achat DOWN | {len(sub):>7,} | {p_down_next*100:>10.2f}% | "
          f"{ev:>+7.2f}pp | {'EDGE' if p_down_next > 0.52 else '':>5}")

print()
# Streak de DOWN -> on attend UP
for streak_len in [1, 2, 3, 4]:
    mask_dn = pd.Series([True] * N, index=df.index)
    for lag in range(1, streak_len + 1):
        mask_dn = mask_dn & (df[f"dir_lag{lag}"] == 0)

    sub = df[mask_dn]
    if len(sub) < 50:
        continue
    p_up_next = sub["dir"].mean()  # P(next = UP)
    ev = (p_up_next - baseline_p) * 100
    print(f"  DOWN x{streak_len:>1} -> achat UP  | {len(sub):>7,} | {p_up_next*100:>10.2f}% | "
          f"{ev:>+7.2f}pp | {'EDGE' if p_up_next > 0.52 else '':>5}")

# ==============================================================
# 2. TAILLE RELATIVE DES BOUGIES SUCCESSIVES
# ==============================================================
print()
print("=" * 60)
print("2. TAILLE RELATIVE : EST-CE QUE LA TENDANCE ACCELERE ?")
print("=" * 60)
print("(Bougie N-2 plus grande que N-1 = momentum qui faiblit = mean-rev plus probable ?)")
print()

# Cas : N-1 UP + N-2 UP, mais |N-2| > |N-1| (momentum qui ralentit)
for dir_streak, label in [(1, "UP x2 decroissant"), (0, "DOWN x2 decroissant")]:
    mask_streak = (df["dir_lag1"] == dir_streak) & (df["dir_lag2"] == dir_streak)
    mask_decay  = df["abs_lag2"] > df["abs_lag1"]  # N-2 > N-1 = ralentissement
    mask_accel  = df["abs_lag1"] > df["abs_lag2"]  # N-1 > N-2 = acceleration

    for desc, mask_size in [("ralentit", mask_decay), ("accelere", mask_accel)]:
        sub = df[mask_streak & mask_size]
        if len(sub) < 50:
            continue
        if dir_streak == 1:
            p_rev = 1 - sub["dir"].mean()
        else:
            p_rev = sub["dir"].mean()
        ev = (p_rev - 0.5) * 100
        print(f"  {label} ({desc}) | n={len(sub):>6,} | P(mean-rev)={p_rev*100:.2f}% | EV={ev:+.2f}pp")

# ==============================================================
# 3. COMBINAISON MAGNITUDE + STREAK
# ==============================================================
print()
print("=" * 60)
print("3. MAGNITUDE + STREAK COMBINE")
print("=" * 60)
print("(Mean-rev plus forte si plusieurs bougies fortes dans la meme direction)")
print()

bins   = [0, 0.10, 0.20, 0.30, 0.50, np.inf]
labels_mag = ["<0.10%", "0.10-0.20%", "0.20-0.30%", "0.30-0.50%", ">0.50%"]

print(f"{'Condition':>40} | {'n':>6} | {'P(mean-rev)':>11} | {'EV (pp)':>8}")
print("-" * 75)

for dir_val, dir_label in [(1, "UP"), (0, "DOWN")]:
    for streak_len in [1, 2, 3]:
        for mag_lbl, lo, hi in [("mod (0.10-0.30%)", 0.10, 0.30),
                                 ("fort (>0.30%)",    0.30, np.inf)]:
            mask = (df["dir_lag1"] == dir_val) & (df["abs_lag1"].between(lo, hi))
            for lag in range(2, streak_len + 1):
                mask = mask & (df[f"dir_lag{lag}"] == dir_val)

            sub = df[mask]
            if len(sub) < 30:
                continue
            p_rev = (1 - sub["dir"].mean()) if dir_val == 1 else sub["dir"].mean()
            ev = (p_rev - 0.5) * 100
            label = f"{dir_label} x{streak_len}, N-1 {mag_lbl}"
            print(f"  {label:>38} | {len(sub):>6,} | {p_rev*100:>10.2f}% | {ev:>+7.2f}pp")

print()

# ==============================================================
# 4. LAGS INDEPENDANTS (N-1 vs N-2 vs N-3)
# ==============================================================
print("=" * 60)
print("4. INFORMATION MARGINALE DE CHAQUE LAG")
print("=" * 60)
print("(Quel lag apporte le plus d info independante ?)")
print()

# Regression logistique simple : chaque lag predit-il la prochaine direction ?
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

y = df["dir"].values

for features, label in [
    (["dir_lag1"],                         "Lag1 seul"),
    (["dir_lag2"],                         "Lag2 seul"),
    (["dir_lag3"],                         "Lag3 seul"),
    (["dir_lag1", "dir_lag2"],             "Lag1 + Lag2"),
    (["dir_lag1", "dir_lag2", "dir_lag3"], "Lag1+2+3"),
    (["dir_lag1", "abs_lag1"],             "Lag1 + magnitude"),
    (["dir_lag1", "abs_lag1", "dir_lag2", "abs_lag2"], "Lag1+2 + magnitudes"),
]:
    X = df[features].values
    model = LogisticRegression(max_iter=200, random_state=0)
    model.fit(X, y)
    auc = roc_auc_score(y, model.predict_proba(X)[:, 1])
    print(f"  {label:>35} | AUC = {auc:.5f}")

# ==============================================================
# 5. MEILLEUR SIGNAL COMPOSITE
# ==============================================================
print()
print("=" * 60)
print("5. MEILLEUR SIGNAL COMPOSITE - TABLEAU RECAPITULATIF")
print("=" * 60)
print()
print("Classement par P(mean-reversion) :")
print()

signals = []

# Tester toutes les combinaisons raisonnables
for streak_len in [1, 2, 3]:
    for dir_val, dir_label in [(1, "UP"), (0, "DOWN")]:
        for mag_lo, mag_hi, mag_label in [
            (0.0, np.inf, "any mag"),
            (0.10, np.inf, ">0.10%"),
            (0.20, np.inf, ">0.20%"),
            (0.30, np.inf, ">0.30%"),
        ]:
            mask = (df["abs_lag1"].between(mag_lo, mag_hi)) & (df["dir_lag1"] == dir_val)
            for lag in range(2, streak_len + 1):
                mask = mask & (df[f"dir_lag{lag}"] == dir_val)

            sub = df[mask]
            if len(sub) < 100:
                continue

            p_rev = (1 - sub["dir"].mean()) if dir_val == 1 else sub["dir"].mean()
            signals.append({
                "signal": f"{dir_label} x{streak_len}, N-1 {mag_label}",
                "n": len(sub),
                "p_mean_rev": p_rev,
                "ev_pp": (p_rev - 0.5) * 100,
            })

df_signals = pd.DataFrame(signals).sort_values("p_mean_rev", ascending=False)
print(df_signals.head(20).to_string(index=False))

print()
print("=" * 60)
print("SYNTHESE POUR LA STRATEGIE")
print("=" * 60)
best = df_signals.iloc[0]
print(f"""
  Meilleur signal : {best['signal']}
    -> P(mean-reversion) = {best['p_mean_rev']*100:.2f}%
    -> EV                = {best['ev_pp']:+.2f}pp par rapport a 50%
    -> Frequence         = {best['n']:,} occurrences sur {N:,} bougies
    -> Freq relative     = {best['n']/N*100:.2f}% du temps
    -> Nb opportunites/j = {best['n'] / (N / (288)):,.1f} (a 5min, 288 bougies/jour)

  Top 5 signaux actionnables (>100 occurrences) :
""")
top5 = df_signals[df_signals["n"] >= 200].head(5)
for _, row in top5.iterrows():
    freq_per_day = row["n"] / (N / 288)
    print(f"    {row['signal']:<40} | EV={row['ev_pp']:+.2f}pp | ~{freq_per_day:.1f}/jour")
