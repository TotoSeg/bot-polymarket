"""
Analyse des marchés disponibles par fenêtre de résolution.
Répond à : combien de marchés S3+SP (hors sport+crypto) dans 48h/72h/etc. ?

Usage :
    python src/phase6_bot/analyze_windows.py
"""

import sys, os
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.phase5_paper.polymarket_client import get_active_markets, parse_yes_price
from src.phase5_paper.strategy_signals  import check_signals, _is_crypto
from src.phase5_paper.paper_portfolio   import _kelly_size, load_portfolio

_env = Path(__file__).parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

PORTFOLIO_FILE = Path(__file__).resolve().parents[2] / "outputs" / "phase6" / "live_portfolio.json"

_KW_SPORT = [
    "football", "soccer", "basketball", "tennis", "nba", "nfl", "nhl", "mlb",
    "premier league", "ligue 1", "serie a", "bundesliga", "la liga",
    "champions league", "world cup", "copa ", "super bowl",
    "goal scorer", "top scorer", "top goal", "golden boot",
    "grand slam", "wimbledon", "formula 1", " f1 ",
    "ufc ", " boxing", "olympics", "rugby ", "cricket", " golf ",
    " fc ", "batting", "pitcher", "quarterback",
]

WINDOWS = [
    ("48h", 48), ("72h", 72), ("96h", 96), ("5j", 120), ("6j", 144), ("7j", 168),
    ("8j", 192), ("9j", 216), ("10j", 240), ("11j", 264), ("12j", 288), ("13j", 312), ("14j", 336),
    ("30j", 720),
]


def _is_sport(q: str) -> bool:
    return any(k in q for k in _KW_SPORT)


def _parse_end_date(m: dict):
    """Identique à live_bot : essaie endDate puis endDateIso."""
    FAR = datetime(9999, 12, 31, tzinfo=timezone.utc)
    raw = m.get("endDate") or m.get("endDateIso") or ""
    if not raw:
        return FAR
    try:
        raw = raw.rstrip("Z").replace("Z", "+00:00")
        dt  = datetime.fromisoformat(raw + ("T00:00:00" if "T" not in raw else ""))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return FAR


def main():
    now = datetime.now(tz=timezone.utc)

    try:
        pf = load_portfolio(PORTFOLIO_FILE)
        capital_commit = sum(p["bet_amount"] for p in pf["positions_ouvertes"].values())
        capital_kelly  = pf["capital_disponible"] + capital_commit
    except Exception:
        capital_kelly = 285.0
        print("[INFO] Portfolio non trouvé → capital hypothétique 285$")

    max_bet = float(os.getenv("MAX_BET_USDC", "200"))

    print(f"\nCapital Kelly : {capital_kelly:.2f}$ | MAX_BET : {max_bet:.0f}$")
    print(f"Date          : {now.strftime('%Y-%m-%d %H:%M')} UTC\n")
    print("Récupération marchés (30s)...")

    markets = get_active_markets(min_volume=500, max_pages=30)


    # ── Comptes intermédiaires pour diagnostic ───────────────────────────────
    n_total = len(markets)
    n_price = n_crypto = n_sport = n_no_date = n_past = n_no_sig = 0
    eligible = []

    for m in markets:
        q  = str(m.get("question", "")).lower()
        yp = parse_yes_price(m)

        if not yp or not (0.05 <= yp <= 0.35):
            n_price += 1; continue
        if _is_crypto(q):
            n_crypto += 1; continue
        if _is_sport(q):
            n_sport += 1; continue

        end_dt      = _parse_end_date(m)
        hours_to_end = (end_dt - now).total_seconds() / 3600

        if end_dt.year >= 9999:
            n_no_date += 1; continue
        if hours_to_end < 0:
            n_past += 1; continue

        sigs = check_signals(m, yp)
        if not sigs:
            n_no_sig += 1; continue

        best = max(sigs, key=lambda s: s["score"])
        bet  = min(_kelly_size(best["win_rate_prior"], yp, capital_kelly), max_bet)

        eligible.append({
            "question": str(m.get("question",""))[:70],
            "yp": yp, "strategy": best["strategy"],
            "win_rate": best["win_rate_prior"], "bet": bet,
            "hours": hours_to_end, "end_dt": end_dt,
        })

    eligible.sort(key=lambda x: x["hours"])

    # ── Diagnostic ───────────────────────────────────────────────────────────
    print(f"Marchés totaux        : {n_total}")
    print(f"  Filtrés (prix)      : {n_price}")
    print(f"  Filtrés (crypto)    : {n_crypto}")
    print(f"  Filtrés (sport)     : {n_sport}")
    print(f"  Sans date           : {n_no_date}")
    print(f"  Déjà expirés        : {n_past}")
    print(f"  Sans signal S3/SP   : {n_no_sig}")
    print(f"  ÉLIGIBLES           : {len(eligible)}\n")

    # ── Tableau par fenêtre ──────────────────────────────────────────────────
    print(f"{'Fenêtre':<8} {'Marchés':>8} {'Bets≥1$':>8} {'Capital déployé':>16} {'% capital':>10}")
    print("=" * 55)

    prev_h = 0
    cumul  = 0.0
    cap_r  = capital_kelly

    for label, h in WINDOWS:
        sub     = [e for e in eligible if prev_h < e["hours"] <= h]
        ok      = [e for e in sub if e["bet"] >= 1.0]
        dep     = min(sum(min(e["bet"], cap_r) for e in ok), cap_r)
        cumul  += dep
        cap_r   = max(0, capital_kelly - cumul)
        pct     = cumul / capital_kelly * 100 if capital_kelly > 0 else 0
        print(f"{label:<8} {len(sub):>8} {len(ok):>8} {cumul:>14.2f}$ {pct:>9.1f}%")
        prev_h = h

    print("=" * 55)

    # ── Types de marchés scannés ─────────────────────────────────────────────
    from collections import Counter
    from src.phase5_paper.strategy_signals import _category
    strats   = Counter(e["strategy"] for e in eligible)
    cats_raw = []
    for m in markets:
        q  = str(m.get("question","")).lower()
        yp = parse_yes_price(m)
        if not yp or not (0.05 <= yp <= 0.35): continue
        if _is_crypto(q) or _is_sport(q): continue
        cats_raw.append(_category(q))
    cats = Counter(cats_raw)

    print(f"\nTypes de marchés éligibles (478 total) :")
    print(f"  S3 (YES 5-10%, hors crypto/sport) : {strats.get('S3', 0)}")
    print(f"  SP (YES 5-35%, politique/géopol)  : {strats.get('SP', 0)}")
    print(f"  Catégories :")
    for cat, n in cats.most_common():
        print(f"    {cat:<25} : {n}")

    # ── Zoom 7-14j ───────────────────────────────────────────────────────────
    zoom = [e for e in eligible if 168 < e["hours"] <= 336]
    if zoom:
        print(f"\nDétail {len(zoom)} marchés dans la fenêtre 7-14j :")
        print(f"{'Strat':<5} {'YES':>5} {'Bet':>6} {'Fin':>12}  Question")
        print("-" * 75)
        for e in zoom:
            print(f"{e['strategy']:<5} {e['yp']:>5.3f} {e['bet']:>5.2f}$  "
                  f"{e['end_dt'].strftime('%Y-%m-%d')}  {e['question'][:42]}")


if __name__ == "__main__":
    main()
