"""Simulation optimisation portefeuille S3/SP — 4 scénarios comparés."""
import sys, duckdb, pandas as pd, numpy as np, matplotlib
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
matplotlib.use("Agg"); import matplotlib.pyplot as plt
from pathlib import Path

con = duckdb.connect()
OUT = Path("outputs/phase3/ptf_simulation")
OUT.mkdir(parents=True, exist_ok=True)

POL = ("trump","election","congress","president","parliament","minister",
       "ceasefire","war","nato","vote","referendum","tariff","sanction",
       "senate","legislation","coup")
GEO = ("ceasefire","war","nato","treaty","nuclear","troops","invasion","coup")
CRYPTO = ("bitcoin"," btc","ethereum"," eth","crypto","up or down")

pol_sql  = " OR ".join(f"LOWER(question) LIKE '%{k}%'" for k in POL)
geo_sql  = " OR ".join(f"LOWER(question) LIKE '%{k}%'" for k in GEO)
cry_sql  = " OR ".join(f"LOWER(question) LIKE '%{k}%'" for k in CRYPTO)

df = con.execute(f"""
WITH avg_p AS (SELECT market_id, AVG(price) as yp FROM read_parquet('data/quant.parquet') GROUP BY market_id)
SELECT m.id, CAST(m.end_date AS DATE) as end_date,
    a.yp as yes_price,
    CASE WHEN m.outcome_prices LIKE '[''1''%' THEN 1 ELSE 0 END as yes_res,
    CASE WHEN ({geo_sql}) THEN 1 ELSE 0 END as is_geo,
    CASE WHEN a.yp BETWEEN 0.05 AND 0.10 AND NOT ({cry_sql}) THEN 'S3'
         WHEN a.yp BETWEEN 0.05 AND 0.35 AND ({pol_sql}) THEN 'SP'
         ELSE NULL END as strat
FROM read_parquet('data/markets.parquet') m
JOIN avg_p a ON m.id=a.market_id
WHERE m.closed=1 AND YEAR(m.end_date) IN (2024,2025)
  AND (m.outcome_prices LIKE '[''1''%' OR m.outcome_prices LIKE '[''0''%')
ORDER BY m.end_date
""").df()
df = df[df["strat"].notna()].copy()

# Combiné : une seule position par marché, pas de double-mise
df_combined = df.drop_duplicates("id", keep="first").reset_index(drop=True)

CAPITAL_INIT = 1000.0
MAX_POS = 40

def fee(yp, geo):
    rate = 0.0 if geo else 0.01
    return (25 / (1 - yp)) * rate * yp * (1 - yp)

def simulate(trades, mode="fixed", fixed_bet=25.0, bet_pct=0.025, max_bet=200.0):
    capital = CAPITAL_INIT
    open_pos = {}
    committed = 0.0
    records = []
    for _, row in trades.iterrows():
        mid = row["id"]; yp = row["yes_price"]
        res = row["yes_res"]; dt  = row["end_date"]
        geo = int(row.get("is_geo", 0))

        if mode == "fixed":
            bet = fixed_bet
        elif mode == "pct":
            bet = max(1.0, min(capital * bet_pct, max_bet))
        elif mode == "stepped":
            mult = min(max(1, int(capital / CAPITAL_INIT)), 8)
            bet  = min(fixed_bet * mult, max_bet)

        if (capital - committed) >= bet and len(open_pos) < MAX_POS and mid not in open_pos:
            open_pos[mid] = (bet, yp, geo)
            committed += bet

        if mid in open_pos:
            b, eyp, egeo = open_pos.pop(mid)
            committed -= b
            f   = (b / (1 - eyp)) * (0.0 if egeo else 0.01) * eyp * (1 - eyp)
            pnl = (b * eyp / (1 - eyp)) - f if res == 0 else -b
            capital += pnl
            records.append({"date": dt, "pnl": pnl, "win": res == 0,
                             "capital": capital, "bet": b})
    return pd.DataFrame(records)

s3   = df[df.strat == "S3"].reset_index(drop=True)
sp   = df[df.strat == "SP"].reset_index(drop=True)
comb = df_combined.reset_index(drop=True)

scenarios = {
    "S3 fixe 25$":              simulate(s3,   "fixed",   fixed_bet=25),
    "SP fixe 25$":              simulate(sp,   "fixed",   fixed_bet=25),
    "S3+SP combine fixe 25$":   simulate(comb, "fixed",   fixed_bet=25),
    "S3+SP combine 2.5% cap200$": simulate(comb, "pct",   bet_pct=0.025, max_bet=200),
}

print("COMPARAISON DES 4 SCENARIOS (2024-2025, depart 1000$)")
print()
for name, r in scenarios.items():
    n    = len(r); wins = r.win.sum()
    pnl  = r.pnl.sum()
    dd   = (r.capital.cummax() - r.capital).max()
    bmin = r.bet.min(); bmax = r.bet.max()
    print(f"{name}")
    print(f"  Trades: {n:,} | WR: {wins/n*100:.1f}% | P&L: +{pnl:,.0f}$ "
          f"| Capital final: {CAPITAL_INIT+pnl:,.0f}$ | ROI: +{pnl/CAPITAL_INIT*100:.0f}%")
    print(f"  Mise: {bmin:.0f}$-{bmax:.0f}$ | Max drawdown: {dd:.0f}$ | Pertes: {(~r.win).sum()}")
    print()

# Plot
fig, axes = plt.subplots(2, 2, figsize=(18, 11))
fig.suptitle("Optimisation strategie : Fixed vs Compound vs Combine (2024-2025, depart 1000$)",
             fontsize=13, fontweight="bold")
colors = ["steelblue","darkorange","green","crimson"]
for ax, (name, r), col in zip(axes.flatten(), scenarios.items(), colors):
    r2 = r.copy(); r2["cum"] = r2.pnl.cumsum()
    dates = pd.to_datetime(r2.date)
    ax.plot(dates, CAPITAL_INIT + r2.cum, color=col, lw=2)
    ax.axhline(CAPITAL_INIT, color="gray", ls="--", alpha=0.5)
    ax.fill_between(dates, CAPITAL_INIT, CAPITAL_INIT + r2.cum, alpha=0.12, color="green")
    losses = r[~r.win]
    if not losses.empty:
        ax.scatter(pd.to_datetime(losses.date), losses.capital, color="red", s=20, zorder=5)
    roi   = r.pnl.sum() / CAPITAL_INIT * 100
    final = CAPITAL_INIT + r.pnl.sum()
    dd    = (r.capital.cummax() - r.capital).max()
    ax.set_title(f"{name}\nFinal: {final:,.0f} USD | ROI: +{roi:.0f}% | DD max: {dd:.0f} USD", fontsize=10)
    ax.set_ylabel("Capital ($)"); ax.grid(alpha=0.3)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
plt.tight_layout()
plt.savefig(OUT / "optimisation_scenarios.png", dpi=150); plt.close()
print(f"Graphique -> {OUT}/optimisation_scenarios.png")

# Progression mensuelle du meilleur scénario
print("\nPROGRESSION MENSUELLE — S3+SP combine 2.5% cap200$")
r_best = scenarios["S3+SP combine 2.5% cap200$"].copy()
r_best["date"] = pd.to_datetime(r_best["date"])
r_best["month"] = r_best["date"].dt.to_period("M")
monthly = r_best.groupby("month").agg(
    trades=("pnl","count"), pnl=("pnl","sum"),
    wins=("win","sum"), capital=("capital","last"), bet_max=("bet","max")
).reset_index()
monthly["wr"] = monthly["wins"] / monthly["trades"] * 100
for _, row in monthly.iterrows():
    print(f"  {row.month} | {row.trades:>4.0f} trades | WR {row.wr:>5.1f}% | "
          f"P&L {row.pnl:>+8.0f}$ | Capital {row.capital:>8,.0f}$ | Mise max {row.bet_max:>6.0f}$")
