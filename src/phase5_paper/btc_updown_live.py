"""
Phase 5 — Paper Trading BTC Up/Down en temps réel
===================================================
Stratégie live : compare le prix BTC réel (Binance, maille 5mn) avec
les prix affichés sur les marchés Polymarket "Up or Down" BTC.

STRATÉGIES IMPLÉMENTÉES :
  SA — Contre-momentum : BTC monte fort → parier NO sur "BTC UP"
  SB — Momentum       : BTC monte fort → parier YES sur "BTC UP"
  SC — Mispricing     : Polymarket sous-évalue le signal BTC → exploiter l'écart

Le script choisit la stratégie optimale par seuil de momentum défini en config.
Les positions sont intégrées au portefeuille paper trading standard (portfolio.json).

Usage :
    # Un scan
    python src/phase5_paper/btc_updown_live.py

    # Avec rapport du snapshot BTC actuel
    python src/phase5_paper/btc_updown_live.py --snapshot

    # Boucle toutes les 5 minutes
    python src/phase5_paper/btc_updown_live.py --loop
"""

import sys
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.phase5_paper.btc_price_feed    import get_btc_snapshot, get_recent_klines, compute_indicators
from src.phase5_paper.polymarket_client import get_active_markets, parse_yes_price, parse_resolution, get_market
from src.phase5_paper.paper_portfolio   import (
    load_portfolio, save_portfolio, add_position, close_position, print_summary
)

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT   = Path(__file__).resolve().parents[2]
LOGS_DIR       = PROJECT_ROOT / "logs"
OUT_DIR        = PROJECT_ROOT / "outputs" / "phase5"
PORTFOLIO_FILE = OUT_DIR / "portfolio.json"

# ── Configuration des stratégies ──────────────────────────────────────────────

# Seuil de mouvement BTC pour déclencher un signal (en %)
MOMENTUM_THRESHOLD_5M  = 0.20   # Mouvement sur 5 min
MOMENTUM_THRESHOLD_30M = 1.00   # Mouvement sur 30 min (optimal backtest : 1.0-1.5 % → WR 71-72%)

# Seuil de mispricing pour SC (différence fair_prob - polymarket_price)
MISPRICING_THRESHOLD = 0.15     # 15% d'écart minimum

# Volume minimum du marché Polymarket pour s'y positionner
MIN_VOLUME_BTC_MARKET = 1_000   # 1K$ minimum de liquidité

# Stratégie active : "SA" (contre-momentum), "SB" (momentum), "SC" (mispricing)
# ou "BEST" pour sélectionner automatiquement
ACTIVE_STRATEGY = "SB"


def setup_logger():
    LOGS_DIR.mkdir(exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_btc_updown.log"
    logger.remove()
    logger.add(sys.stdout, colorize=True, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
    logger.add(log_file, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")


# ── Détection de signal BTC ────────────────────────────────────────────────────

def get_btc_context() -> dict:
    """
    Récupère le contexte BTC actuel depuis Binance.

    Retourne un dict avec :
      price, ret_5m, ret_30m, vol_ratio, direction, signal_strength
    """
    snap = get_btc_snapshot()

    # Force du signal (0 à 1) basée sur le momentum 30m et la volatilité
    ret_30m     = snap.get("ret_30m") or 0.0
    vol_ratio   = snap.get("vol_ratio") or 1.0
    sig_strength = min(abs(ret_30m) / 2.0, 1.0)  # 2% de mouvement = force 1.0

    snap["signal_strength"] = round(sig_strength, 3)

    logger.info(f"BTC snapshot : prix={snap['price']:,.0f}$ | "
                f"ret_5m={snap.get('ret_5m', 0):+.2f}% | "
                f"ret_30m={ret_30m:+.2f}% | "
                f"vol_ratio={vol_ratio:.2f} | "
                f"force_signal={sig_strength:.2f}")
    return snap


# ── Identification des marchés BTC Up/Down actifs ─────────────────────────────

def get_btc_updown_markets() -> list[dict]:
    """
    Récupère les marchés Polymarket "BTC Up or Down" actifs.
    Filtre sur les mots-clés bitcoin + up or down.
    """
    all_markets = get_active_markets(min_volume=MIN_VOLUME_BTC_MARKET, max_pages=20)

    btc_markets = []
    for m in all_markets:
        q = str(m.get("question", "")).lower()
        if ("bitcoin" in q or " btc" in q) and ("up or down" in q or "higher" in q or "lower" in q):
            yes_price = parse_yes_price(m)
            if yes_price is not None and 0.05 <= yes_price <= 0.95:
                m["_yes_price"] = yes_price
                btc_markets.append(m)

    logger.info(f"Marchés BTC Up/Down actifs trouvés : {len(btc_markets)}")
    for m in btc_markets[:5]:
        logger.info(f"  {m.get('question','')[:60]:60s} | YES={m['_yes_price']:.3f}")
    return btc_markets


# ── Génération des signaux par stratégie ──────────────────────────────────────

def _fair_prob_from_btc(ret_30m: float) -> float:
    """Probabilité 'juste' que BTC soit UP, basée sur le retour 30m."""
    import math
    # Sigmoid : 1% de mouvement ↔ ~15pp de probabilité supplémentaire
    return 1 / (1 + math.exp(-5 * ret_30m / 100))


def generate_signals(btc_ctx: dict, market: dict) -> list[dict]:
    """
    Génère les signaux pour un marché BTC Up/Down selon la stratégie active.

    Retourne une liste de signaux :
      [{"strategy": "SC-BTC", "side": "YES"|"NO", "win_rate_prior": 0.X,
        "reason": "...", "score": X}]
    """
    signals = []
    yes_price    = market["_yes_price"]
    ret_30m      = btc_ctx.get("ret_30m") or 0.0
    ret_5m       = btc_ctx.get("ret_5m")  or 0.0
    vol_ratio    = btc_ctx.get("vol_ratio") or 1.0
    sig_strength = btc_ctx.get("signal_strength", 0.0)

    # ── SA : Contre-momentum ──────────────────────────────────────────────────
    if ACTIVE_STRATEGY in ("SA", "BEST"):
        if ret_30m > MOMENTUM_THRESHOLD_30M:
            # BTC monte → parier NO (DOWN) — reversion attendue
            signals.append({
                "strategy":       "BTC-SA",
                "side":           "NO",
                "win_rate_prior": 0.52 + sig_strength * 0.08,  # entre 52% et 60%
                "reason":         f"SA contre-momentum BTC +{ret_30m:.2f}% → bet NO",
                "score":          sig_strength * 5,
            })
        elif ret_30m < -MOMENTUM_THRESHOLD_30M:
            signals.append({
                "strategy":       "BTC-SA",
                "side":           "YES",
                "win_rate_prior": 0.52 + sig_strength * 0.08,
                "reason":         f"SA contre-momentum BTC {ret_30m:.2f}% → bet YES",
                "score":          sig_strength * 5,
            })

    # ── SB : Momentum ─────────────────────────────────────────────────────────
    if ACTIVE_STRATEGY in ("SB", "BEST"):
        if ret_30m > MOMENTUM_THRESHOLD_30M:
            signals.append({
                "strategy":       "BTC-SB",
                "side":           "YES",
                "win_rate_prior": 0.52 + sig_strength * 0.08,
                "reason":         f"SB momentum BTC +{ret_30m:.2f}% → bet YES",
                "score":          sig_strength * 4,
            })
        elif ret_30m < -MOMENTUM_THRESHOLD_30M:
            signals.append({
                "strategy":       "BTC-SB",
                "side":           "NO",
                "win_rate_prior": 0.52 + sig_strength * 0.08,
                "reason":         f"SB momentum BTC {ret_30m:.2f}% → bet NO",
                "score":          sig_strength * 4,
            })

    # ── SC : Mispricing ───────────────────────────────────────────────────────
    if ACTIVE_STRATEGY in ("SC", "BEST"):
        fair_prob = _fair_prob_from_btc(ret_30m)
        mispricing = fair_prob - yes_price  # positif → Polymarket sous-évalue YES

        if abs(ret_30m) >= MOMENTUM_THRESHOLD_30M * 0.5:  # BTC en mouvement
            if mispricing > MISPRICING_THRESHOLD:
                # Polymarket pense YES trop peu probable vs signal BTC → acheter YES
                edge = mispricing  # edge estimé
                signals.append({
                    "strategy":       "BTC-SC",
                    "side":           "YES",
                    "win_rate_prior": min(0.50 + edge * 1.5, 0.75),
                    "reason":         (f"SC mispricing: BTC={ret_30m:+.2f}%, "
                                       f"fair={fair_prob:.2f}, poly={yes_price:.2f}, "
                                       f"edge={mispricing:+.2f}"),
                    "score":          min(mispricing * 30, 9),
                })
            elif mispricing < -MISPRICING_THRESHOLD:
                # Polymarket sur-évalue YES → parier NO
                edge = abs(mispricing)
                signals.append({
                    "strategy":       "BTC-SC",
                    "side":           "NO",
                    "win_rate_prior": min(0.50 + edge * 1.5, 0.75),
                    "reason":         (f"SC mispricing: BTC={ret_30m:+.2f}%, "
                                       f"fair={fair_prob:.2f}, poly={yes_price:.2f}, "
                                       f"edge={mispricing:+.2f}"),
                    "score":          min(abs(mispricing) * 30, 9),
                })

    return signals


# ── Intégration avec le portfolio ─────────────────────────────────────────────

def add_btc_position(portfolio: dict, market: dict, signal: dict):
    """
    Enregistre une position BTC Up/Down dans le portfolio.

    Pour les marchés BTC, on adapte l'entrée selon le côté (YES ou NO) :
      - side="YES" → on achète YES, on gagne si outcome=1
      - side="NO"  → on achète NO (= vendre YES), on gagne si outcome=0
    """
    market_id = str(market.get("id", ""))
    btc_key   = f"BTC_{signal['side']}_{market_id}"  # Clé unique côté

    # Adapter le market dict pour le portfolio
    market_copy = dict(market)
    market_copy["id"] = btc_key
    market_copy["question"] = (
        f"[BTC {signal['side']}] " + str(market.get("question", ""))[:60]
    )

    yes_price = market["_yes_price"]
    if signal["side"] == "NO":
        # On mise sur NO : le "prix" du pari NO = 1 - yes_price
        entry_price = 1.0 - yes_price
        # win_rate_prior reste tel quel (on gagne si outcome=0)
    else:
        entry_price = yes_price

    return add_position(
        portfolio      = portfolio,
        market         = market_copy,
        strategy       = signal["strategy"],
        win_rate_prior = signal["win_rate_prior"],
        entry_price_yes= entry_price,
        reason         = signal["reason"],
    )


# ── Résolution des positions BTC ───────────────────────────────────────────────

def resolve_btc_positions(portfolio: dict):
    """
    Vérifie la résolution des marchés BTC Up/Down ouverts.
    Les clés BTC_YES_xxx et BTC_NO_xxx sont traitées séparément.
    """
    btc_positions = {
        mid: pos for mid, pos in portfolio["positions_ouvertes"].items()
        if pos.get("strategy", "").startswith("BTC-")
    }

    if not btc_positions:
        return 0

    logger.info(f"Vérification de {len(btc_positions)} positions BTC...")
    nb_closed = 0

    for btc_key, pos in btc_positions.items():
        # Extraire le vrai market_id depuis la clé "BTC_YES_<market_id>"
        parts = btc_key.split("_", 2)
        if len(parts) < 3:
            continue
        side      = parts[1]   # YES ou NO
        market_id = parts[2]

        market = get_market(market_id)
        if market is None:
            continue

        outcome = parse_resolution(market)
        if outcome is None:
            continue

        # Adapter l'outcome selon le côté
        # side=YES → on gagne si outcome=1 (YES wins)
        # side=NO  → on gagne si outcome=0 (NO wins), mais le moteur du portfolio
        #            attend que outcome=0 soit un "win NO" (ce qui est notre cas)
        # Le portfolio interprète toujours : outcome=0 → NO gagne = on gagne
        # Pour BTC-YES : on gagne si YES gagne → on doit avoir outcome=1 → loss pour le moteur
        # MAIS on a stocké entry_price_yes = yes_price pour YES, = 1-yes_price pour NO
        # Le moteur calcule P&L selon outcome → on garde l'outcome brut du marché

        close_position(portfolio, btc_key, outcome)
        nb_closed += 1
        time.sleep(0.1)

    return nb_closed


# ── MAIN ──────────────────────────────────────────────────────────────────────

def run_once():
    """Exécute un cycle complet de la stratégie BTC."""
    logger.info("=" * 60)
    logger.info(f"BTC Up/Down LIVE — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info(f"Stratégie active : {ACTIVE_STRATEGY}")
    logger.info("=" * 60)

    portfolio = load_portfolio(PORTFOLIO_FILE)

    # 1. Résoudre les positions BTC ouvertes
    resolve_btc_positions(portfolio)

    # 2. Snapshot BTC actuel
    btc_ctx = get_btc_context()

    if btc_ctx.get("signal_strength", 0) < 0.05:
        logger.info("Signal BTC trop faible (< 0.05) — pas de nouveaux trades")
    else:
        # 3. Marchés Polymarket BTC actifs
        btc_markets = get_btc_updown_markets()

        nb_new = 0
        for market in btc_markets:
            signals = generate_signals(btc_ctx, market)
            if not signals:
                continue

            # Prendre le signal avec le meilleur score
            best = max(signals, key=lambda s: s.get("score", 0))
            added = add_btc_position(portfolio, market, best)
            if added:
                nb_new += 1

        logger.info(f"Nouvelles positions BTC : {nb_new}")

    # 4. Sauvegarder
    save_portfolio(portfolio, PORTFOLIO_FILE)
    print_summary(portfolio)
    logger.success("Cycle BTC terminé.")


def main():
    setup_logger()

    parser = argparse.ArgumentParser(description="Paper trading BTC Up/Down")
    parser.add_argument("--snapshot", action="store_true",
                        help="Afficher seulement le snapshot BTC actuel")
    parser.add_argument("--loop",     action="store_true",
                        help="Tourner en boucle toutes les 5 minutes")
    args = parser.parse_args()

    if args.snapshot:
        ctx = get_btc_context()
        klines = get_recent_klines("5m", n=12)
        if klines is not None:
            klines = compute_indicators(klines)
            logger.info("\nDernières 12 bougies 5m :")
            logger.info(klines[["open_time", "close", "return_pct", "vol_ratio"]].tail(12).to_string())
        return

    if args.loop:
        logger.info("Mode boucle : scan toutes les 5 minutes")
        while True:
            run_once()
            logger.info("Prochain scan dans 5 minutes (Ctrl+C pour arrêter)")
            time.sleep(300)
    else:
        run_once()


if __name__ == "__main__":
    main()
