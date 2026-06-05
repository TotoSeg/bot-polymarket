"""
Backtest 3 strategies sur signal mean-reversion — tous assets, deux directions
===============================================================================
Signal : 3 bougies consecutives dans la meme direction + derniere > p90

S1 : Achat du cote prevu gagnant a ~50c, tenir jusqu'a resolution
S2 : S1 + hedge de 1$ quand le cote oppose tombe a 30c (notre cote = 70%)
S3 : S2 + revente de notre position a 95c, hedge conserve jusqu'a resolution

Mise principale : 2$  |  Hedge : 1$  |  Frais : 2%

Lecture des cas :
  CAS A : aucun trigger — prix ne monte pas a 70% (on perd si signal faux)
  CAS B : hedge declenche (70%) mais 95% jamais atteint
  CAS C : les deux triggers se declenchent (hedge + vente a 95%)

Pour comprendre les pourcentages :
  '34% des trades tombent en CAS A'  = proportion de TOUS les trades
  '82% perdent en CAS A'             = WR de 18% = proportion qui perd DANS ce cas
"""
import sys
from pathlib import Path
import duckdb
import pandas as pd
import numpy as np

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FEE            = 0.02
BET            = 2.0
HEDGE_AMOUNT   = 1.0
HEDGE_TRIG     = 0.70
SELL_TRIG      = 0.95
ENTRY_MIN      = 0.40
ENTRY_MAX      = 0.60
HEDGE_MIN_SECS = 30

conn = duckdb.connect()

# ================================================================
# CHARGEMENT DES DONNEES POLYMARKET
# ================================================================
print("Chargement Polymarket...")
ticks = conn.execute("""
    SELECT t.market_id, t.outcome, t.price, t.timestamp_ms,
           m.resolution, m.end_ts, m.question,
           (m.end_ts * 1000 - t.timestamp_ms) / 1000.0 AS secs_remaining
    FROM read_parquet('data/updown/ticks_5m.parquet') t
    INNER JOIN read_parquet('data/updown/markets_5m.parquet') m
        ON t.market_id = m.market_id
    WHERE t.timestamp_ms > 0
      AND t.price BETWEEN 0.01 AND 0.99
      AND m.resolution IN (0, 1)
      AND (m.end_ts * 1000 - t.timestamp_ms) / 1000.0 BETWEEN 0 AND 300
    ORDER BY t.market_id, t.timestamp_ms
""").df()

markets = conn.execute("""
    SELECT market_id, end_ts, resolution, question
    FROM read_parquet('data/updown/markets_5m.parquet')
    WHERE resolution IN (0, 1)
""").df()

# Identifier l'asset de chaque marche depuis la question
def detect_asset(q):
    q = str(q).lower()
    if "bitcoin" in q or "btc" in q:  return "BTC"
    if "ethereum" in q or "eth" in q: return "ETH"
    if "solana" in q or "sol" in q:   return "SOL"
    if "xrp" in q or "ripple" in q:   return "XRP"
    if "doge" in q:                   return "DOGE"
    if "bnb" in q:                    return "BNB"
    return "OTHER"

markets["asset"] = markets["question"].apply(detect_asset)
print(f"  Marches par asset :")
print(markets.groupby("asset").size().to_string())

# ================================================================
# CHARGEMENT DES BOUGIES PAR ASSET
# ================================================================

CANDLE_FILES = {
    "BTC":  "data/updown/btc_5m_candles.parquet",
    "ETH":  "data/updown/eth_5m_candles.parquet",
    "SOL":  "data/updown/sol_5m_candles.parquet",
    "XRP":  "data/updown/xrp_5m_candles.parquet",
    "DOGE": "data/updown/doge_5m_candles.parquet",
}

candle_data = {}  # asset -> (candle_idx, p90_threshold)

for asset, path in CANDLE_FILES.items():
    if not Path(path).exists():
        print(f"  {asset} : fichier candles absent ({path}) — ignore")
        continue
    df_c = pd.read_parquet(path)
    df_c["open_ts"] = df_c["open_time"].astype("int64") // 1000
    df_c["ret"]     = (df_c["close"] - df_c["open"]) / df_c["open"] * 100
    df_c["dir"]     = (df_c["close"] > df_c["open"]).astype(int)
    df_c["ret_abs"] = df_c["ret"].abs()
    df_c = df_c.sort_values("open_ts").drop_duplicates(subset=["open_ts"]).reset_index(drop=True)
    idx  = df_c.set_index("open_ts")
    cutoff = df_c["open_time"].max() - pd.Timedelta(days=90)
    p90    = df_c[df_c["open_time"] >= cutoff]["ret_abs"].quantile(0.90)
    candle_data[asset] = (idx, p90)
    print(f"  {asset} : {len(df_c):,} bougies | p90 = {p90:.4f}%")

# ================================================================
# SIGNAL PAR ASSET
# ================================================================

def get_signal(end_ts, candle_idx, p90):
    try:
        c1 = candle_idx.loc[end_ts - 600]
        c2 = candle_idx.loc[end_ts - 900]
        c3 = candle_idx.loc[end_ts - 1200]
    except KeyError:
        return 0
    if float(c1["ret_abs"]) < p90:
        return 0
    dirs = [int(c3["dir"]), int(c2["dir"]), int(c1["dir"])]
    if all(d == 0 for d in dirs): return  1  # 3 DOWN -> acheter UP
    if all(d == 1 for d in dirs): return -1  # 3 UP   -> acheter DOWN
    return 0

print("\nCalcul des signaux...")
signal_map = {}
for asset, (idx, p90) in candle_data.items():
    mkt_asset = markets[markets["asset"] == asset]
    for _, row in mkt_asset.iterrows():
        sig = get_signal(int(row["end_ts"]), idx, p90)
        if sig != 0:
            signal_map[row["market_id"]] = (sig, asset)

print(f"  Total signaux : {len(signal_map)}")
for asset in candle_data:
    n = sum(1 for v in signal_map.values() if v[1] == asset)
    up   = sum(1 for v in signal_map.values() if v[1] == asset and v[0] == 1)
    down = sum(1 for v in signal_map.values() if v[1] == asset and v[0] == -1)
    print(f"    {asset} : {n} signaux ({up} UP / {down} DOWN)")

# ================================================================
# SIMULATION
# ================================================================

def simulate(group, signal):
    group = group.sort_values("timestamp_ms").reset_index(drop=True)
    resolution   = int(group["resolution"].iloc[0])
    notre_cote   = "Up"   if signal ==  1 else "Down"
    signal_gagne = (signal == 1 and resolution == 1) or (signal == -1 and resolution == 0)

    first = group[group["outcome"] == notre_cote].head(1)
    if first.empty: return None
    prix_entree = first["price"].iloc[0]
    if not (ENTRY_MIN <= prix_entree <= ENTRY_MAX): return None

    tokens_achetes = BET / prix_entree
    hedge_place    = False
    hedge_prix     = 0.0
    hedge_tokens   = 0.0
    vente_exec     = False
    vente_prix     = 0.0

    for _, row in group.iterrows():
        if row["outcome"] != notre_cote: continue
        p = row["price"]
        secs = row["secs_remaining"]
        if not hedge_place and p >= HEDGE_TRIG and secs > HEDGE_MIN_SECS:
            hedge_prix   = 1.0 - p
            hedge_tokens = HEDGE_AMOUNT / hedge_prix
            hedge_place  = True
        if hedge_place and not vente_exec and p >= SELL_TRIG:
            vente_prix = p
            vente_exec = True

    # P&L position principale
    if vente_exec:
        pnl_principal = tokens_achetes * vente_prix * (1 - FEE) - BET
    elif signal_gagne:
        pnl_principal = tokens_achetes * (1 - prix_entree) * (1 - FEE)
    else:
        pnl_principal = -BET

    # P&L hedge
    if hedge_place:
        if signal_gagne:
            pnl_hedge = -HEDGE_AMOUNT
        else:
            pnl_hedge = hedge_tokens * (1 - hedge_prix) * (1 - FEE)
    else:
        pnl_hedge = 0.0

    # S1 : sans hedge, sans vente
    s1 = tokens_achetes * (1 - prix_entree) * (1 - FEE) if signal_gagne else -BET

    # S2 : hedge, tenu jusqu'a resolution (pas de vente a 95%)
    if hedge_place:
        s2_principal = tokens_achetes * (1 - prix_entree) * (1 - FEE) if signal_gagne else -BET
        s2 = s2_principal + pnl_hedge
    else:
        s2 = s1

    # S3 : vente a 95% + hedge conserve
    s3 = pnl_principal + pnl_hedge

    return {
        "signal_gagne":  int(signal_gagne),
        "signal_dir":    "DOWN_streak" if signal == 1 else "UP_streak",
        "hedge_place":   int(hedge_place),
        "vente_exec":    int(vente_exec),
        "capital":       BET + (HEDGE_AMOUNT if hedge_place else 0.0),
        "s1": round(s1, 4), "s2": round(s2, 4), "s3": round(s3, 4),
    }

print("Simulation...")
all_results = []
ticks_indexed = ticks.set_index("market_id")

for market_id, (sig, asset) in signal_map.items():
    if market_id not in ticks_indexed.index: continue
    grp = ticks.loc[ticks["market_id"] == market_id]
    r = simulate(grp, sig)
    if r:
        r["asset"] = asset
        all_results.append(r)

df = pd.DataFrame(all_results)
n  = len(df)
print(f"  Marches simules : {n}\n")

if n == 0:
    print("Aucun resultat. Verifier les donnees.")
    exit()

# ================================================================
# AFFICHAGE
# ================================================================

SEP = "=" * 72

def stats_block(sub, label=""):
    if len(sub) == 0:
        return
    n_sub = len(sub)
    for strat, col in [("S1 (sans hedge)         ", "s1"),
                       ("S2 (hedge a 70%)        ", "s2"),
                       ("S3 (hedge 70%+vente 95%)", "s3")]:
        ev  = sub[col].mean()
        wr  = (sub[col] > 0).mean() * 100
        roi = sub[col].sum() / (sub["capital"].sum() if col != "s1" else BET * n_sub) * 100
        print(f"    {strat}  EV {ev:>+6.3f}$   WR {wr:>4.0f}%   ROI {roi:>+6.1f}%")

def print_cas(df_sub, titre, pct_total):
    print(f"\n  {titre}")
    print(f"  {len(df_sub)} trades = {pct_total:.0f}% de tous les trades "
          f"| Signal correct dans ce cas : {df_sub['signal_gagne'].mean()*100:.0f}%")
    stats_block(df_sub)

# ── GLOBAL ──────────────────────────────────────────────────────
print(SEP)
print("BILAN GLOBAL")
print(SEP)
stats_block(df)
print(f"\n  Signal BTC correct  : {df['signal_gagne'].mean()*100:.1f}%  ({df['signal_gagne'].sum()}/{n})")
print(f"  Hedge declenche     : {df['hedge_place'].mean()*100:.1f}%  ({df['hedge_place'].sum()}/{n})")
print(f"  Vente 95% executee  : {df['vente_exec'].mean()*100:.1f}%  ({df['vente_exec'].sum()}/{n})")

# ── EXPLICATION DES CAS ─────────────────────────────────────────
print(f"\n{SEP}")
print("DETAIL PAR CAS")
print(f"{SEP}")
print("""
  Comment lire les cas :
  - CAS A : le prix ne monte pas a 70% dans les 5 min -> pas de hedge, pas de vente
            on gagne si le signal est correct, on perd -2$ sinon
  - CAS B : le prix atteint 70% puis redescend avant 95%
            le hedge est declenche et nous sauve si la bougie se retourne
  - CAS C : le prix monte jusqu'a 95%
            on vend notre position, on encaisse le profit
            le hedge reste ouvert en cas de retournement spectaculaire
""")

cas_a = df[(df["hedge_place"] == 0)]
cas_b = df[(df["hedge_place"] == 1) & (df["vente_exec"] == 0)]
cas_c = df[(df["hedge_place"] == 1) & (df["vente_exec"] == 1)]

print_cas(cas_a, "CAS A — prix ne depasse jamais 70%", len(cas_a)/n*100)
print_cas(cas_b, "CAS B — prix atteint 70% mais pas 95%", len(cas_b)/n*100)
print_cas(cas_c, "CAS C — prix atteint 70% puis 95%", len(cas_c)/n*100)

# ── CAS C DETAIL ────────────────────────────────────────────────
if len(cas_c) > 0:
    print(f"\n{SEP}")
    print("CAS C EN DETAIL")
    print(SEP)
    print("""
  Rappel : dans ce cas tu as deja vendu ta position principale a 95c.
  Tu n'as plus que le hedge ouvert. Deux issues possibles a la resolution :""")
    for gagne, lbl in [
        (1, "Bougie se resout comme prevu (notre cote gagne)"),
        (0, "Retournement : la bougie repart en sens inverse apres 95%"),
    ]:
        sub = cas_c[cas_c["signal_gagne"] == gagne]
        if len(sub) == 0: continue
        print(f"\n  {lbl}  —  {len(sub)} cas sur {len(cas_c)}")
        print(f"  S1 : {sub['s1'].mean():>+.3f}$ moyen  (sans hedge, sans vente : tu aurais tenu jusqu'au bout)")
        print(f"  S2 : {sub['s2'].mean():>+.3f}$ moyen  (hedge declenche, pas de vente)")
        print(f"  S3 : {sub['s3'].mean():>+.3f}$ moyen  (vente a 95% + hedge jusqu'a resolution)")

# ── PAR DIRECTION DU SIGNAL ─────────────────────────────────────
print(f"\n{SEP}")
print("PAR DIRECTION DU SIGNAL")
print(SEP)
for direction, lbl in [("DOWN_streak", "3 bougies DOWN -> achat UP"),
                        ("UP_streak",   "3 bougies UP   -> achat DOWN")]:
    sub = df[df["signal_dir"] == direction]
    if len(sub) == 0: continue
    print(f"\n  {lbl}  ({len(sub)} trades)")
    stats_block(sub)

# ── PAR ASSET ───────────────────────────────────────────────────
print(f"\n{SEP}")
print("PAR ASSET")
print(SEP)
for asset in ["BTC", "ETH", "SOL", "XRP", "DOGE"]:
    sub = df[df["asset"] == asset]
    if len(sub) == 0: continue
    print(f"\n  {asset}  ({len(sub)} trades | signal correct : {sub['signal_gagne'].mean()*100:.0f}%)")
    stats_block(sub)

# ── SYNTHESE ────────────────────────────────────────────────────
print(f"\n{SEP}")
print("SYNTHESE")
print(SEP)
print(f"""
  Mise : {BET}$  |  Hedge : {HEDGE_AMOUNT}$  |  Total max par trade : {BET+HEDGE_AMOUNT}$
  Signal : 3 bougies consecutives + derniere bougie > p90

  {'Strategie':<30}  {'EV/trade':>9}  {'WR':>6}
  {'-'*50}""")
for lbl, col in [("S1  Sans hedge",              "s1"),
                 ("S2  Hedge a 70%",              "s2"),
                 ("S3  Hedge 70% + vente 95%",    "s3")]:
    ev = df[col].mean()
    wr = (df[col] > 0).mean() * 100
    print(f"  {lbl:<30}  {ev:>+8.3f}$  {wr:>5.0f}%")

print(f"\n  ATTENTION : {n} trades au total (donnees limitees a 3-4 jours).")
