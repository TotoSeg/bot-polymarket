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

WINDOWS = [("48h",48),("72h",72),("96h",96),("5j",120),("7j",168),("14j",336),("30j",720)]


def _is_sport(q: str) -> bool:
    return any(k in q for k in _KW_SPORT)


def _parse_end_date(m: dict):
    """Identique à live_bot : essaie endDate puis end_date_iso."""
    FAR = datetime(9999, 12, 31, tzinfo=timezone.utc)
    raw = m.get("endDate") or m.get("end_date_iso") or ""
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

    # ── DEBUG : afficher les clés et prix du premier marché ──────────────────
    if markets:
        m0 = markets[0]
        print(f"\n[DEBUG] Premier marché : {str(m0.get('question',''))[:60]}")
        price_fields = {k: v for k, v in m0.items()
                        if any(x in k.lower() for x in ["price", "outcome", "token"])}
        print(f"[DEBUG] Champs prix    : {price_fields}")
        print(f"[DEBUG] parse_yes_price → {parse_yes_price(m0)}\n")

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

    # ── Détail des 20 premiers marchés éligibles ─────────────────────────────
    if eligible:
        print(f"\n20 premiers marchés éligibles (sur {len(eligible)}) :")
        print(f"{'Strat':<5} {'YES':>5} {'Bet':>6} {'Heures':>7}  Question")
        print("-" * 75)
        for e in eligible[:20]:
            print(f"{e['strategy']:<5} {e['yp']:>5.3f} {e['bet']:>5.2f}$ {e['hours']:>6.0f}h  "
                  f"{e['end_dt'].strftime('%m-%d %H:%M')}  {e['question'][:40]}")
    else:
        print("\nAucun marché éligible trouvé.")
        print("Vérifie que le filtre sport n'est pas trop agressif (voir compteurs ci-dessus).")


if __name__ == "__main__":
    main()
