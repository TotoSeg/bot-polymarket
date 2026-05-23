"""
Explication numérique du Kelly sizing avec les valeurs réelles du portefeuille.
Usage : python3 src/phase6_bot/kelly_explainer.py
"""

import json, sys
from pathlib import Path

PORTFOLIO_FILE = Path(__file__).resolve().parents[2] / "outputs" / "phase6" / "live_portfolio.json"
POLYMARKET_FEE = 0.02
KELLY_FRACTION  = 0.25   # on joue 25% du Kelly théorique (prudence)
KELLY_CAP       = 0.05   # f* plafonné à 5% du capital
MAX_BET         = 25.0   # mise max absolue en $

SEP  = "=" * 60
SEP2 = "-" * 60

# ── 1. Charger le portefeuille ────────────────────────────────
d = json.load(open(PORTFOLIO_FILE))
positions   = d.get("positions_ouvertes", {})
capital_dispo = d.get("capital_disponible", 0)

valeur_face = sum(
    p["bet_amount"] / max(1.0 - p.get("entry_price_yes", 0.5), 0.001)
    for p in positions.values()
)
capital_kelly = capital_dispo + valeur_face

print(f"\n{SEP}")
print("  KELLY SIZING — EXPLICATION NUMÉRIQUE")
print(SEP)

# ── 2. Capital de base ────────────────────────────────────────
print(f"""
┌─ CAPITAL DE RÉFÉRENCE (= valeur totale du portefeuille)
│
│   Capital disponible (USDC liquide)    : {capital_dispo:>8.2f}$
│   Valeur face positions ouvertes       : {valeur_face:>8.2f}$
│     (= tokens NO détenus × 1$ payout)
│                                          ────────
│   Capital Kelly total                  : {capital_kelly:>8.2f}$
└─
""")

# ── 3. Formule Kelly pour 2 exemples ─────────────────────────
examples = [
    {"label": "S3  (YES = 6%,  win rate 99.4%)", "yes": 0.06,  "wr": 0.994},
    {"label": "SP  (YES = 27%, win rate 94.9%)", "yes": 0.27,  "wr": 0.949},
]

print(SEP2)
print("  FORMULE KELLY")
print(SEP2)
print("""
  On cherche la fraction f* du capital à miser pour maximiser
  la croissance à long terme (critère de Kelly).

  Données d'entrée :
    p  = probabilité de gagner (win rate historique)
    q  = 1 - p  (probabilité de perdre)
    b  = gain net si NO gagne / mise investie
       = prix_YES / prix_NO = YES / (1 - YES)

  Formule :
    f* = (p × b - q) / b

  Puis on applique deux garde-fous :
    1. Plafond à 5% du capital  → f* = min(f*, 0.05)
    2. Fraction de Kelly à 25%  → mise = f* × capital × 0.25
    3. Plafond absolu           → mise = min(mise, MAX_BET$)
    4. Plafond capital dispo    → mise = min(mise, capital_dispo)
""")

for ex in examples:
    yes = ex["yes"]
    wr  = ex["wr"]
    no  = 1.0 - yes
    b   = yes / no
    q   = 1.0 - wr

    f_brut  = (wr * b - q) / b
    f_cap   = min(max(f_brut, 0), KELLY_CAP)
    mise    = f_cap * capital_kelly * KELLY_FRACTION
    mise    = min(mise, MAX_BET)
    mise    = min(mise, capital_dispo)

    print(f"  ┌─ {ex['label']}")
    print(f"  │   YES = {yes:.2f}   →   NO = {no:.2f}")
    print(f"  │   win rate p = {wr:.3f}   →   q = {q:.3f}")
    print(f"  │")
    print(f"  │   b = YES/NO = {yes:.2f}/{no:.2f} = {b:.4f}")
    print(f"  │     (si NO gagne sur mise 1$ → gain net {b:.4f}$)")
    print(f"  │")
    print(f"  │   f* brut = (p×b - q) / b")
    print(f"  │           = ({wr:.3f}×{b:.4f} - {q:.3f}) / {b:.4f}")
    print(f"  │           = ({wr*b:.4f} - {q:.3f}) / {b:.4f}")
    print(f"  │           = {f_brut:.4f}  ({f_brut*100:.1f}% du capital)")
    print(f"  │")
    if f_brut > KELLY_CAP:
        print(f"  │   ⚠ Plafonné à {KELLY_CAP*100:.0f}%  →  f* = {KELLY_CAP:.2f}")
    else:
        print(f"  │   f* < 5% → pas de plafonnement")
    print(f"  │")
    print(f"  │   Mise = f* × capital × fraction Kelly")
    print(f"  │        = {f_cap:.2f} × {capital_kelly:.2f}$ × {KELLY_FRACTION}")
    print(f"  │        = {f_cap * capital_kelly * KELLY_FRACTION:.2f}$")
    if mise < f_cap * capital_kelly * KELLY_FRACTION:
        print(f"  │   ⚠ Limité par MAX_BET={MAX_BET}$ ou capital dispo {capital_dispo:.2f}$")
    print(f"  │")
    print(f"  └─  MISE FINALE : {mise:.2f}$")
    print()

# ── 4. Résumé des positions actuelles ────────────────────────
print(SEP2)
print("  POSITIONS OUVERTES — KELLY APPLIQUÉ")
print(SEP2)
print(f"  {'Strat':<5} {'YES':>5} {'NO':>5} {'WR':>6} {'b':>6} {'f*':>6} {'Mise':>7}  Question")
print(f"  {'-'*5} {'-'*5} {'-'*5} {'-'*6} {'-'*6} {'-'*6} {'-'*7}  {'-'*35}")

for mid, pos in list(positions.items())[:15]:
    yes = pos.get("entry_price_yes", 0)
    wr  = pos.get("win_rate_prior", 0)
    bet = pos.get("bet_amount", 0)
    no  = 1.0 - yes
    b   = yes / no if no > 0 else 0
    q   = 1.0 - wr
    f   = min(max((wr * b - q) / b if b > 0 else 0, 0), KELLY_CAP)
    strat = pos.get("strategy", "?")
    q_txt = pos.get("question", "")[:38]
    print(f"  {strat:<5} {yes:>5.3f} {no:>5.3f} {wr:>6.3f} {b:>6.3f} {f*100:>5.1f}%  {bet:>6.2f}$  {q_txt}")

print(f"\n  Capital Kelly : {capital_kelly:.2f}$  |  Dispo : {capital_dispo:.2f}$  |  MAX_BET : {MAX_BET}$")
print(f"  Fraction Kelly appliquée : {KELLY_FRACTION*100:.0f}%  |  Plafond f* : {KELLY_CAP*100:.0f}%\n")
